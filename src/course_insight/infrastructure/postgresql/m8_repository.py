"""PostgreSQL persistence for M8 papers, audits, and scoring histories."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.base import ContractModel
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool


_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M8 repository integrity check failed"
_CONFLICT_ERROR = "PostgreSQL M8 repository identity conflict"
_CHECKSUM_ERROR = "PostgreSQL M8 persisted payload checksum mismatch"
_SCHEMA_ERROR = "PostgreSQL M8 persisted schema version mismatch"
_PAPER_FREEZE_ERROR = "PostgreSQL M8 paper immutable checksum mismatch"
_TContract = TypeVar("_TContract", bound=ContractModel)

_PAPER_COLUMNS = """
paper_id,
task_id,
course_id,
class_id,
learner_id,
payload,
payload_checksum,
schema_version
"""
_AUDIT_COLUMNS = """
audit_id,
audit_version,
item_instance_id,
payload,
payload_checksum,
schema_version
"""
_SCORING_COLUMNS = """
attempt_id,
result_key,
paper_id,
learner_id,
finalized_at,
payload,
payload_checksum,
schema_version
"""


class PostgresM8Repository:
    """Persist append-only M8 recovery objects without exposing rows."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def insert_or_get_paper(
        self,
        paper: AssessmentPaper,
        *,
        course_id: str,
        class_id: str,
    ) -> AssessmentPaper:
        """Persist a frozen paper and its authoritative execution scope."""

        if not _nonblank(course_id) or not _nonblank(class_id):
            raise ValueError("M8 paper execution scope must not be blank")
        candidate = _isolated_contract(paper, AssessmentPaper)
        if candidate.immutable_checksum != candidate.freeze():
            raise ValueError("M8 paper immutable checksum is invalid")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO m8_assessment_papers(
                            paper_id,
                            task_id,
                            course_id,
                            class_id,
                            learner_id,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            candidate.paper_id,
                            candidate.task_id,
                            course_id,
                            class_id,
                            candidate.learner_id,
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_PAPER_COLUMNS}
                        FROM m8_assessment_papers
                        WHERE paper_id = %s OR task_id = %s
                        ORDER BY
                            CASE WHEN paper_id = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (
                            candidate.paper_id,
                            candidate.task_id,
                            candidate.paper_id,
                        ),
                    ).fetchone()
                    stored = _paper_from_row(row)
                    stored_scope = (
                        _required_text(row, "course_id"),
                        _required_text(row, "class_id"),
                    )
                    if (
                        stored != candidate
                        or stored_scope != (course_id, class_id)
                    ):
                        raise PostgresOperationError(_CONFLICT_ERROR)
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_paper(self, paper: AssessmentPaper) -> None:
        """Retain the legacy write only when scope already exists."""

        context = self.get_paper_execution_context(paper.paper_id)
        if context is None:
            raise ValueError(
                "M8 paper persistence requires course and class execution scope"
            )
        self.insert_or_get_paper(
            paper,
            course_id=context[0],
            class_id=context[1],
        )

    def get_paper(self, paper_id: str) -> AssessmentPaper | None:
        """Load one immutable paper by identity."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_PAPER_COLUMNS}
                    FROM m8_assessment_papers
                    WHERE paper_id = %s
                    """,
                    (paper_id,),
                ).fetchone()
                return None if row is None else _paper_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_paper_execution_context(
        self,
        paper_id: str,
    ) -> tuple[str, str] | None:
        """Load the internal course/class event scope for one paper."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT course_id, class_id
                    FROM m8_assessment_papers
                    WHERE paper_id = %s
                    """,
                    (paper_id,),
                ).fetchone()
                if row is None:
                    return None
                return (
                    _required_text(row, "course_id"),
                    _required_text(row, "class_id"),
                )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None
        except Exception:
            raise PostgresOperationError(_INTEGRITY_ERROR) from None

    def save_score_audit(self, record: ScoreAuditRecord) -> None:
        """Append one immutable audit version."""

        candidate = _isolated_contract(record, ScoreAuditRecord)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _insert_or_validate_audit(connection, candidate)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_score_audit(
        self,
        audit_id: str,
        audit_version: int,
    ) -> ScoreAuditRecord | None:
        """Load one exact audit version."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_AUDIT_COLUMNS}
                    FROM m8_score_audits
                    WHERE audit_id = %s AND audit_version = %s
                    """,
                    (audit_id, audit_version),
                ).fetchone()
                return None if row is None else _audit_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_scoring_result(
        self,
        bundle: ScoringResultBundle,
    ) -> ScoringResultBundle:
        """Persist one audit-vector version as a single transaction."""

        candidate = _isolated_contract(bundle, ScoringResultBundle)
        candidate.validate_business_rules()
        result_key = _result_key(candidate)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    for record in candidate.score_audit_records:
                        _insert_or_validate_audit(connection, record)
                    connection.execute(
                        """
                        INSERT INTO m8_scoring_results(
                            attempt_id,
                            result_key,
                            paper_id,
                            learner_id,
                            finalized_at,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (attempt_id, result_key) DO NOTHING
                        """,
                        (
                            candidate.attempt_id,
                            result_key,
                            candidate.paper_id,
                            candidate.learner_id,
                            candidate.finalized_at,
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_SCORING_COLUMNS}
                        FROM m8_scoring_results
                        WHERE attempt_id = %s AND result_key = %s
                        """,
                        (candidate.attempt_id, result_key),
                    ).fetchone()
                    stored = _scoring_from_row(row)
                    if stored != candidate:
                        raise PostgresOperationError(_CONFLICT_ERROR)
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_scoring_result(self, bundle: ScoringResultBundle) -> None:
        """Retain the compatibility write alias."""

        self.insert_or_get_scoring_result(bundle)

    def get_scoring_result(
        self,
        attempt_id: str,
    ) -> ScoringResultBundle | None:
        """Load the latest result by real TIMESTAMPTZ order."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_SCORING_COLUMNS}
                    FROM m8_scoring_results
                    WHERE attempt_id = %s
                    ORDER BY finalized_at DESC, result_key DESC
                    LIMIT 1
                    """,
                    (attempt_id,),
                ).fetchone()
                return None if row is None else _scoring_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_scoring_result_by_checksum(
        self,
        attempt_id: str,
        checksum: str,
    ) -> ScoringResultBundle | None:
        """Load an exact historical result by canonical contract checksum."""

        for bundle in self._scoring_history(attempt_id):
            if bundle.content_checksum() == checksum:
                return bundle
        return None

    def get_scoring_result_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> ScoringResultBundle | None:
        """Load the earliest result whose target audit is at the exact version."""

        for bundle in self._scoring_history(attempt_id):
            if any(
                record.audit_id == audit_id
                and record.audit_version == audit_version
                for record in bundle.score_audit_records
            ):
                return bundle
        return None

    def _scoring_history(
        self,
        attempt_id: str,
    ) -> list[ScoringResultBundle]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_SCORING_COLUMNS}
                    FROM m8_scoring_results
                    WHERE attempt_id = %s
                    ORDER BY finalized_at ASC, result_key ASC
                    """,
                    (attempt_id,),
                ).fetchall()
                return [_scoring_from_row(row) for row in rows]
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _insert_or_validate_audit(
    connection: Any,
    record: ScoreAuditRecord,
) -> None:
    candidate = _isolated_contract(record, ScoreAuditRecord)
    connection.execute(
        """
        INSERT INTO m8_score_audits(
            audit_id,
            audit_version,
            item_instance_id,
            payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (audit_id, audit_version) DO NOTHING
        """,
        (
            candidate.audit_id,
            candidate.audit_version,
            candidate.item_instance_id,
            Jsonb(candidate.to_dict()),
            candidate.content_checksum(),
            candidate.schema_version,
        ),
    )
    row = connection.execute(
        f"""
        SELECT {_AUDIT_COLUMNS}
        FROM m8_score_audits
        WHERE audit_id = %s AND audit_version = %s
        """,
        (candidate.audit_id, candidate.audit_version),
    ).fetchone()
    if row is None or _audit_from_row(row) != candidate:
        raise PostgresOperationError(_CONFLICT_ERROR)


def _result_key(bundle: ScoringResultBundle) -> str:
    latest_versions: dict[str, int] = {}
    for record in bundle.score_audit_records:
        latest_versions = {
            **latest_versions,
            record.audit_id: max(
                record.audit_version,
                latest_versions.get(record.audit_id, 0),
            ),
        }
    return dumps_json(
        [
            {"audit_id": audit_id, "audit_version": audit_version}
            for audit_id, audit_version in sorted(latest_versions.items())
        ]
    )


def _paper_from_row(row: Mapping[str, Any] | None) -> AssessmentPaper:
    paper = _contract_from_row(row, AssessmentPaper, label="paper")
    try:
        if paper.immutable_checksum != paper.freeze():
            raise PostgresOperationError(_PAPER_FREEZE_ERROR)
        if (
            paper.paper_id != _required_text(row, "paper_id")
            or paper.task_id != _required_text(row, "task_id")
            or paper.learner_id != _required_text(row, "learner_id")
        ):
            raise ValueError
        _required_text(row, "course_id")
        _required_text(row, "class_id")
        return paper
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _audit_from_row(row: Mapping[str, Any] | None) -> ScoreAuditRecord:
    record = _contract_from_row(
        row,
        ScoreAuditRecord,
        label="score audit",
    )
    try:
        if (
            record.audit_id != _required_text(row, "audit_id")
            or record.audit_version != _positive_int(row, "audit_version")
            or record.item_instance_id
            != _required_text(row, "item_instance_id")
        ):
            raise ValueError
        return record
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _scoring_from_row(
    row: Mapping[str, Any] | None,
) -> ScoringResultBundle:
    bundle = _contract_from_row(
        row,
        ScoringResultBundle,
        label="scoring result",
    )
    try:
        if (
            bundle.attempt_id != _required_text(row, "attempt_id")
            or bundle.paper_id != _required_text(row, "paper_id")
            or bundle.learner_id != _required_text(row, "learner_id")
            or _result_key(bundle) != _required_text(row, "result_key")
            or bundle.finalized_at != _required_datetime(row, "finalized_at")
        ):
            raise ValueError
        return bundle
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
        raise ValueError("M8 contract schema version is unsupported")
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


PostgreSQLM8Repository = PostgresM8Repository

__all__ = ["PostgresM8Repository", "PostgreSQLM8Repository"]
