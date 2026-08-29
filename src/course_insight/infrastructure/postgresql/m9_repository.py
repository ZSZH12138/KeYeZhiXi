"""PostgreSQL persistence for M9 analytics and teacher reviews."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import hmac
import json
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.contracts.base import ContractModel
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.infrastructure.postgresql.actor_erasure import purge_postgres_actor
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
    M9ReviewDecisionConflict,
    analytics_learner_scope,
)


_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M9 repository integrity check failed"
_CONFLICT_ERROR = "PostgreSQL M9 repository identity conflict"
_CHECKSUM_ERROR = "PostgreSQL M9 persisted payload checksum mismatch"
_SCHEMA_ERROR = "PostgreSQL M9 persisted schema version mismatch"
_TContract = TypeVar("_TContract", bound=ContractModel)

_ANALYTICS_COLUMNS = """
report_id,
course_id,
class_id,
generated_at,
learner_ids,
payload,
payload_checksum,
schema_version
"""
_REVIEW_COLUMNS = """
decision_id,
audit_id,
expected_audit_version,
payload,
payload_checksum,
schema_version
"""
_MODEL_AUDIT_COLUMNS = """
invocation_id,
request_id,
source_report_id,
source_report_checksum,
scope,
provider,
model_name,
provider_status,
validation_status,
created_at,
payload,
payload_checksum
"""


class PostgresM9Repository:
    """Persist scoped analytics and optimistic-version review decisions."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def purge_actor(self, actor_id: str) -> int:
        return purge_postgres_actor(self._pool, module="m9", actor_id=actor_id)

    def save_model_audit(self, record: M9ModelAuditRecord) -> None:
        """Persist one idempotent privacy-minimized model-call record."""

        if not isinstance(record, M9ModelAuditRecord):
            raise TypeError("record must be an M9ModelAuditRecord")
        payload = record.to_dict()
        checksum = _model_audit_checksum(payload)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    authoritative_source_checksum = _source_report_checksum(
                        connection,
                        record.source_report_id,
                    )
                    if not hmac.compare_digest(
                        record.source_report_checksum,
                        authoritative_source_checksum,
                    ):
                        raise PostgresOperationError(_CHECKSUM_ERROR)
                    connection.execute(
                        """
                        INSERT INTO m9_model_invocation_audits(
                            invocation_id,
                            request_id,
                            source_report_id,
                            source_report_checksum,
                            scope,
                            provider,
                            model_name,
                            provider_status,
                            validation_status,
                            created_at,
                            payload,
                            payload_checksum
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            record.invocation_id,
                            record.request_id,
                            record.source_report_id,
                            record.source_report_checksum,
                            record.scope,
                            record.provider,
                            record.model_name,
                            record.provider_status,
                            record.validation_status,
                            record.created_at,
                            Jsonb(payload),
                            checksum,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_MODEL_AUDIT_COLUMNS}
                        FROM m9_model_invocation_audits
                        WHERE invocation_id = %s OR request_id = %s
                        ORDER BY
                            CASE WHEN invocation_id = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (
                            record.invocation_id,
                            record.request_id,
                            record.invocation_id,
                        ),
                    ).fetchone()
                    if _model_audit_from_row(
                        row,
                        authoritative_source_checksum=authoritative_source_checksum,
                    ) != record:
                        raise PostgresOperationError(_CONFLICT_ERROR)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_model_audit(
        self,
        invocation_id: str,
    ) -> M9ModelAuditRecord | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_MODEL_AUDIT_COLUMNS}
                    FROM m9_model_invocation_audits
                    WHERE invocation_id = %s
                    """,
                    (invocation_id,),
                ).fetchone()
                if row is None:
                    return None
                authoritative_source_checksum = _source_report_checksum(
                    connection,
                    _required_text(row, "source_report_id"),
                )
                return _model_audit_from_row(
                    row,
                    authoritative_source_checksum=authoritative_source_checksum,
                )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str,
        learner_scope_ids: Sequence[str] | None = None,
    ) -> TeacherAnalyticsBundle:
        """Persist a report in its M9-owned course/class scope."""

        if not _nonblank(course_id):
            raise ValueError("M9 analytics course scope must not be blank")
        candidate = _isolated_contract(bundle, TeacherAnalyticsBundle)
        learner_ids = analytics_learner_scope(
            candidate,
            learner_scope_ids,
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO m9_teacher_analytics(
                            report_id,
                            course_id,
                            class_id,
                            generated_at,
                            learner_ids,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (report_id) DO NOTHING
                        """,
                        (
                            candidate.report_id,
                            course_id,
                            candidate.class_report.class_id,
                            candidate.generated_at,
                            Jsonb(list(learner_ids)),
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_ANALYTICS_COLUMNS}
                        FROM m9_teacher_analytics
                        WHERE report_id = %s
                        """,
                        (candidate.report_id,),
                    ).fetchone()
                    stored = _analytics_from_row(row)
                    if (
                        stored != candidate
                        or _required_text(row, "course_id") != course_id
                        or _analytics_learner_scope_from_row(row, stored)
                        != learner_ids
                    ):
                        raise PostgresOperationError(_CONFLICT_ERROR)
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str | None = None,
    ) -> None:
        """Require explicit course scope for the compatibility write hook."""

        if course_id is None:
            raise ValueError("M9 analytics persistence requires course scope")
        self.insert_or_get_analytics(bundle, course_id=course_id)

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        """Load one report by stable identity."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_ANALYTICS_COLUMNS}
                    FROM m9_teacher_analytics
                    WHERE report_id = %s
                    """,
                    (report_id,),
                ).fetchone()
                return None if row is None else _analytics_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_scoped_analytics(
        self,
        report_id: str,
        *,
        course_id: str,
        class_id: str,
    ) -> TeacherAnalyticsBundle | None:
        """Load one report only when course and class authorization match."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_ANALYTICS_COLUMNS}
                    FROM m9_teacher_analytics
                    WHERE report_id = %s
                      AND course_id = %s
                      AND class_id = %s
                    """,
                    (report_id, course_id, class_id),
                ).fetchone()
                return None if row is None else _analytics_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        """Load the newest report by real TIMESTAMPTZ within exact scope."""

        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_ANALYTICS_COLUMNS}
                    FROM m9_teacher_analytics
                    WHERE course_id = %s AND class_id = %s
                    ORDER BY generated_at DESC, report_id DESC
                    """,
                    (course_id, class_id),
                ).fetchall()
                for row in rows:
                    bundle = _analytics_from_row(row)
                    learner_scope = _analytics_learner_scope_from_row(
                        row,
                        bundle,
                    )
                    if learner_id is None or learner_id in learner_scope:
                        return bundle
                return None
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_review_decision(
        self,
        decision: TeacherReviewDecision,
    ) -> TeacherReviewDecision:
        """Persist a decision under both identity and audit-version keys."""

        candidate = _isolated_contract(decision, TeacherReviewDecision)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO m9_teacher_reviews(
                            decision_id,
                            audit_id,
                            expected_audit_version,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            candidate.decision_id,
                            candidate.audit_id,
                            candidate.expected_audit_version,
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_REVIEW_COLUMNS}
                        FROM m9_teacher_reviews
                        WHERE decision_id = %s
                           OR (
                                audit_id = %s
                                AND expected_audit_version = %s
                           )
                        ORDER BY
                            CASE WHEN decision_id = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (
                            candidate.decision_id,
                            candidate.audit_id,
                            candidate.expected_audit_version,
                            candidate.decision_id,
                        ),
                    ).fetchone()
                    stored = _review_from_row(row)
                    if stored != candidate:
                        raise M9ReviewDecisionConflict(
                            audit_id=candidate.audit_id,
                            expected_audit_version=(
                                candidate.expected_audit_version
                            ),
                        )
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_review_decision(self, decision: TeacherReviewDecision) -> None:
        """Persist through the authoritative insert-or-get path."""

        self.insert_or_get_review_decision(decision)

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        """Load one teacher decision by stable identity."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_REVIEW_COLUMNS}
                    FROM m9_teacher_reviews
                    WHERE decision_id = %s
                    """,
                    (decision_id,),
                ).fetchone()
                return None if row is None else _review_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _source_report_checksum(
    connection: Any,
    source_report_id: str,
) -> str:
    row = connection.execute(
        f"""
        SELECT {_ANALYTICS_COLUMNS}
        FROM m9_teacher_analytics
        WHERE report_id = %s
        """,
        (source_report_id,),
    ).fetchone()
    bundle = _analytics_from_row(row)
    if bundle.report_id != source_report_id:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return bundle.content_checksum()


def _model_audit_from_row(
    row: Mapping[str, Any] | None,
    *,
    authoritative_source_checksum: str,
) -> M9ModelAuditRecord:
    try:
        if row is None or type(row.get("payload")) is not dict:
            raise ValueError
        payload = row["payload"]
        if not hmac.compare_digest(
            _model_audit_checksum(payload),
            _required_checksum(row, "payload_checksum"),
        ):
            raise PostgresOperationError(_CHECKSUM_ERROR)
        record = M9ModelAuditRecord.from_dict(payload)
        stored_source_checksum = _required_checksum(
            row,
            "source_report_checksum",
        )
        if (
            record.invocation_id != _required_text(row, "invocation_id")
            or record.request_id != _required_text(row, "request_id")
            or record.source_report_id != _required_text(
                row,
                "source_report_id",
            )
            or not hmac.compare_digest(
                record.source_report_checksum,
                stored_source_checksum,
            )
            or not hmac.compare_digest(
                stored_source_checksum,
                authoritative_source_checksum,
            )
            or record.scope != _required_text(row, "scope")
            or record.provider != _required_text(row, "provider")
            or record.model_name != _required_text(row, "model_name")
            or record.provider_status
            != _required_text(row, "provider_status")
            or record.validation_status
            != _required_text(row, "validation_status")
            or record.created_at != _required_datetime(row, "created_at")
        ):
            raise ValueError
        return record
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _model_audit_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _analytics_learner_scope_from_row(
    row: Mapping[str, Any],
    bundle: TeacherAnalyticsBundle,
) -> tuple[str, ...]:
    raw_scope = row.get("learner_ids")
    if type(raw_scope) is not list:
        raise ValueError("M9 analytics learner scope is invalid")
    return analytics_learner_scope(bundle, raw_scope)


def _analytics_from_row(
    row: Mapping[str, Any] | None,
) -> TeacherAnalyticsBundle:
    bundle = _contract_from_row(
        row,
        TeacherAnalyticsBundle,
        label="analytics",
    )
    try:
        if (
            bundle.report_id != _required_text(row, "report_id")
            or bundle.class_report.class_id
            != _required_text(row, "class_id")
            or bundle.generated_at != _required_datetime(row, "generated_at")
        ):
            raise ValueError
        _analytics_learner_scope_from_row(row, bundle)
        _required_text(row, "course_id")
        return bundle
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _review_from_row(
    row: Mapping[str, Any] | None,
) -> TeacherReviewDecision:
    decision = _contract_from_row(
        row,
        TeacherReviewDecision,
        label="teacher review",
    )
    try:
        if (
            decision.decision_id != _required_text(row, "decision_id")
            or decision.audit_id != _required_text(row, "audit_id")
            or decision.expected_audit_version
            != _positive_int(row, "expected_audit_version")
        ):
            raise ValueError
        return decision
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _contract_from_row(
    row: Mapping[str, Any] | None,
    contract_type: type[_TContract],
    *,
    label: str,
) -> _TContract:
    try:
        if row is None or type(row.get("payload")) is not dict:
            raise ValueError
        contract = contract_type.model_validate(row["payload"])
        expected_schema = str(
            contract_type.model_fields["schema_version"].default
        )
        if (
            contract.schema_version != expected_schema
            or _required_text(row, "schema_version") != expected_schema
        ):
            raise PostgresOperationError(f"{_SCHEMA_ERROR}: {label}")
        if contract.content_checksum() != _required_checksum(
            row,
            "payload_checksum",
        ):
            raise PostgresOperationError(f"{_CHECKSUM_ERROR}: {label}")
        return contract
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _isolated_contract(
    value: _TContract,
    contract_type: type[_TContract],
) -> _TContract:
    if not isinstance(value, contract_type):
        raise TypeError(f"value must be a {contract_type.__name__}")
    candidate = contract_type.model_validate(value.model_dump(mode="python"))
    expected_schema = str(
        contract_type.model_fields["schema_version"].default
    )
    if candidate.schema_version != expected_schema:
        raise ValueError("M9 contract schema version is unsupported")
    return candidate


def _required_text(row: Mapping[str, Any] | None, field: str) -> str:
    if row is None:
        raise ValueError
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError
    return value


def _positive_int(row: Mapping[str, Any] | None, field: str) -> int:
    if row is None:
        raise ValueError
    value = row[field]
    if type(value) is not int or value < 1:
        raise ValueError
    return value


def _required_datetime(
    row: Mapping[str, Any] | None,
    field: str,
) -> datetime:
    if row is None:
        raise ValueError
    value = row[field]
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError
    return value


def _required_checksum(row: Mapping[str, Any], field: str) -> str:
    value = _required_text(row, field)
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError
    return value


def _nonblank(value: object) -> bool:
    return type(value) is str and bool(value.strip())


PostgreSQLM9Repository = PostgresM9Repository

__all__ = ["PostgresM9Repository", "PostgreSQLM9Repository"]
