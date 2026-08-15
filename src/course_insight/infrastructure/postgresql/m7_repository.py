"""PostgreSQL persistence for M7 student-feedback recovery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import hmac
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.intelligence import (
    LLMGenerationResult,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.tutoring import (
    STUDENT_CITATION_QUOTE_PLACEHOLDER,
    StudentFeedbackPackage,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.modules.m7_local_model.repository import M7ModelAuditRecord


_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M7 repository integrity check failed"
_CONFLICT_ERROR = "PostgreSQL M7 feedback identity conflict"
_CHECKSUM_ERROR = "PostgreSQL M7 persisted payload checksum mismatch"
_SCHEMA_ERROR = "PostgreSQL M7 persisted schema version mismatch"
_EXPECTED_SCHEMA_VERSION = str(
    StudentFeedbackPackage.model_fields["schema_version"].default
)
_FEEDBACK_COLUMNS = """
feedback_id,
task_id,
learner_id,
payload,
payload_checksum,
schema_version
"""
_AUDIT_COLUMNS = """
invocation_id,
request_id,
scoring_task_id,
provider,
model_name,
provider_status,
validation_status,
privacy_decision,
created_at,
payload,
payload_checksum
"""


class PostgresM7Repository:
    """Persist M7 feedback and privacy-minimized model-call audits."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def save_model_audit(
        self,
        audit_id: str,
        scoring_result: RubricScoringResult,
    ) -> None:
        """Keep the existing optional hook storage-neutral in this milestone."""

        del audit_id, scoring_result

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        """Keep the existing optional hook storage-neutral in this milestone."""

        del prompt_id, prompt_payload

    def save_generation_result(
        self,
        result: LLMGenerationResult,
    ) -> None:
        """Keep sanitized generation persistence optional in this milestone."""

        del result

    def save_invocation_audit(
        self,
        audit: ModelInvocationAudit,
    ) -> None:
        """Keep sanitized invocation persistence optional in this milestone."""

        del audit

    def save_safety_check(
        self,
        result: SafetyCheckResult,
    ) -> None:
        """Keep sanitized safety persistence optional in this milestone."""

        del result

    def save_execution_audit(self, record: M7ModelAuditRecord) -> None:
        """Atomically insert or verify one idempotent model-call audit."""

        if not isinstance(record, M7ModelAuditRecord):
            raise TypeError("record must be an M7ModelAuditRecord")
        payload = record.to_dict()
        checksum = _json_payload_checksum(payload)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO m7_model_invocation_audits(
                            invocation_id,
                            request_id,
                            scoring_task_id,
                            provider,
                            model_name,
                            provider_status,
                            validation_status,
                            privacy_decision,
                            created_at,
                            payload,
                            payload_checksum
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            record.invocation_id,
                            record.request_id,
                            record.scoring_task_id,
                            record.provider,
                            record.model_name,
                            record.provider_status,
                            record.validation_status,
                            record.privacy_decision,
                            record.created_at,
                            Jsonb(payload),
                            checksum,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_AUDIT_COLUMNS}
                        FROM m7_model_invocation_audits
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
                    if _execution_audit_from_row(row) != record:
                        raise PostgresOperationError(_CONFLICT_ERROR)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_execution_audit(
        self,
        invocation_id: str,
    ) -> M7ModelAuditRecord | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_AUDIT_COLUMNS}
                    FROM m7_model_invocation_audits
                    WHERE invocation_id = %s
                    """,
                    (invocation_id,),
                ).fetchone()
                return None if row is None else _execution_audit_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def purge_execution_audits_before(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("M7 audit purge cutoff must be timezone-aware")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    rows = connection.execute(
                        """
                        DELETE FROM m7_model_invocation_audits
                        WHERE created_at < %s
                        RETURNING invocation_id
                        """,
                        (cutoff,),
                    ).fetchall()
                    return len(rows)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_feedback(
        self,
        package: StudentFeedbackPackage,
    ) -> StudentFeedbackPackage:
        """Insert a task/learner package or return its identical winner."""

        candidate = _isolated_feedback(package)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO m7_student_feedback(
                            feedback_id,
                            task_id,
                            learner_id,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            candidate.feedback_id,
                            candidate.task_id,
                            candidate.learner_id,
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_FEEDBACK_COLUMNS}
                        FROM m7_student_feedback
                        WHERE feedback_id = %s
                           OR (task_id = %s AND learner_id = %s)
                        ORDER BY
                            CASE WHEN feedback_id = %s THEN 0 ELSE 1 END
                        LIMIT 1
                        """,
                        (
                            candidate.feedback_id,
                            candidate.task_id,
                            candidate.learner_id,
                            candidate.feedback_id,
                        ),
                    ).fetchone()
                    stored = _feedback_from_row(row)
                    if stored != candidate:
                        raise PostgresOperationError(_CONFLICT_ERROR)
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_feedback(self, package: StudentFeedbackPackage) -> None:
        """Persist feedback through the authoritative insert-or-get path."""

        self.insert_or_get_feedback(package)

    def get_feedback(
        self,
        feedback_id: str,
    ) -> StudentFeedbackPackage | None:
        """Load a feedback package by stable identity."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_FEEDBACK_COLUMNS}
                    FROM m7_student_feedback
                    WHERE feedback_id = %s
                    """,
                    (feedback_id,),
                ).fetchone()
                return None if row is None else _feedback_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_feedback_for_task(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        """Load the unique package for one task and learner."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_FEEDBACK_COLUMNS}
                    FROM m7_student_feedback
                    WHERE task_id = %s AND learner_id = %s
                    """,
                    (task_id, learner_id),
                ).fetchone()
                return None if row is None else _feedback_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_feedback_by_task_and_learner(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        """Retain the compatibility recovery alias."""

        return self.get_feedback_for_task(task_id, learner_id)


def _isolated_feedback(
    package: StudentFeedbackPackage,
) -> StudentFeedbackPackage:
    if not isinstance(package, StudentFeedbackPackage):
        raise TypeError("package must be a StudentFeedbackPackage")
    candidate = StudentFeedbackPackage.model_validate(
        package.model_dump(mode="python")
    )
    if candidate.schema_version != _EXPECTED_SCHEMA_VERSION:
        raise ValueError("M7 feedback schema version is unsupported")
    return candidate


def _feedback_from_row(
    row: Mapping[str, Any] | None,
) -> StudentFeedbackPackage:
    try:
        if row is None or type(row.get("payload")) is not dict:
            raise ValueError
        raw_payload = row["payload"]
        if not hmac.compare_digest(
            _feedback_payload_checksum(raw_payload),
            _required_checksum(row, "payload_checksum"),
        ):
            raise PostgresOperationError(_CHECKSUM_ERROR)
        payload = json.loads(
            json.dumps(raw_payload, ensure_ascii=False, allow_nan=False)
        )
        _migrate_legacy_quotes(payload)
        package = StudentFeedbackPackage.model_validate(payload)
        stored_schema = _required_text(row, "schema_version")
        if (
            package.schema_version != _EXPECTED_SCHEMA_VERSION
            or stored_schema != _EXPECTED_SCHEMA_VERSION
        ):
            raise PostgresOperationError(_SCHEMA_ERROR)
        if (
            package.feedback_id != _required_text(row, "feedback_id")
            or package.task_id != _required_text(row, "task_id")
            or package.learner_id != _required_text(row, "learner_id")
        ):
            raise ValueError
        return package
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _execution_audit_from_row(
    row: Mapping[str, Any] | None,
) -> M7ModelAuditRecord:
    try:
        if row is None or type(row.get("payload")) is not dict:
            raise ValueError
        payload = row["payload"]
        if not hmac.compare_digest(
            _json_payload_checksum(payload),
            _required_checksum(row, "payload_checksum"),
        ):
            raise PostgresOperationError(_CHECKSUM_ERROR)
        record = M7ModelAuditRecord.from_dict(payload)
        if (
            record.invocation_id != _required_text(row, "invocation_id")
            or record.request_id != _required_text(row, "request_id")
            or record.scoring_task_id != _required_text(row, "scoring_task_id")
            or record.provider != _required_text(row, "provider")
            or record.model_name != _required_text(row, "model_name")
            or record.provider_status != _required_text(
                row,
                "provider_status",
            )
            or record.validation_status != _required_text(
                row,
                "validation_status",
            )
            or record.privacy_decision != _required_text(
                row,
                "privacy_decision",
            )
            or record.created_at != _required_datetime(row, "created_at")
        ):
            raise ValueError
        return record
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _json_payload_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _feedback_payload_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migrate_legacy_quotes(payload: dict[str, Any]) -> None:
    """Normalize pre-branch and branch-era frozen-v1 citation shapes."""

    citations = payload.get("evidence_citations")
    if type(citations) is not list:
        return
    quote_presence = tuple(
        type(citation) is dict and "quote" in citation
        for citation in citations
    )
    if any(quote_presence) and not all(quote_presence):
        raise ValueError("feedback citation quote shape is mixed")
    for citation in citations:
        if type(citation) is not dict:
            raise ValueError("feedback citation is invalid")
        if "quote" in citation:
            quote = citation["quote"]
            if type(quote) is not str or not quote.strip():
                raise ValueError("feedback citation quote is invalid")
        citation["quote"] = STUDENT_CITATION_QUOTE_PLACEHOLDER


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row[field]
    if type(value) is not str or not value:
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


def _required_datetime(row: Mapping[str, Any], field: str) -> datetime:
    value = row[field]
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError
    return value


PostgreSQLM7Repository = PostgresM7Repository

__all__ = ["PostgresM7Repository", "PostgreSQLM7Repository"]
