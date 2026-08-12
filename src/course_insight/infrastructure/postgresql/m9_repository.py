"""PostgreSQL persistence for M9 analytics and teacher reviews."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
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
from course_insight.modules.m9_teacher_analytics.repository import (
    ReviewDecisionConflictError,
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


class PostgresM9Repository:
    """Persist scoped analytics and optimistic-version review decisions."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def insert_or_get_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str,
    ) -> TeacherAnalyticsBundle:
        """Persist a report in its M9-owned course/class scope."""

        if not _nonblank(course_id):
            raise ValueError("M9 analytics course scope must not be blank")
        candidate = _isolated_contract(bundle, TeacherAnalyticsBundle)
        learner_ids = sorted(
            report.learner_id for report in candidate.individual_reports
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
                            Jsonb(learner_ids),
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

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        """Load the newest report by real TIMESTAMPTZ within exact scope."""

        learner_filter = ""
        parameters: tuple[Any, ...] = (course_id, class_id)
        if learner_id is not None:
            learner_filter = "AND learner_ids @> %s"
            parameters = (*parameters, Jsonb([learner_id]))
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_ANALYTICS_COLUMNS}
                    FROM m9_teacher_analytics
                    WHERE course_id = %s AND class_id = %s
                    {learner_filter}
                    ORDER BY generated_at DESC, report_id DESC
                    LIMIT 1
                    """,
                    parameters,
                ).fetchone()
                return None if row is None else _analytics_from_row(row)
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
                        raise ReviewDecisionConflictError(_CONFLICT_ERROR)
                    return stored
        except (PostgresError, ReviewDecisionConflictError):
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


def _analytics_from_row(
    row: Mapping[str, Any] | None,
) -> TeacherAnalyticsBundle:
    bundle = _contract_from_row(
        row,
        TeacherAnalyticsBundle,
        label="analytics",
    )
    try:
        expected_learner_ids = sorted(
            report.learner_id for report in bundle.individual_reports
        )
        if (
            bundle.report_id != _required_text(row, "report_id")
            or bundle.class_report.class_id
            != _required_text(row, "class_id")
            or bundle.generated_at != _required_datetime(row, "generated_at")
            or row["learner_ids"] != expected_learner_ids
        ):
            raise ValueError
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
