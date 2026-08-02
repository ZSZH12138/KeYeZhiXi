"""PostgreSQL persistence for M7 student-feedback recovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.intelligence import (
    LLMGenerationResult,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.tutoring import StudentFeedbackPackage
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool


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


class PostgresM7Repository:
    """Persist M7 feedback without adding model-execution behavior."""

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
        package = StudentFeedbackPackage.model_validate(row["payload"])
        stored_schema = _required_text(row, "schema_version")
        if (
            package.schema_version != _EXPECTED_SCHEMA_VERSION
            or stored_schema != _EXPECTED_SCHEMA_VERSION
        ):
            raise PostgresOperationError(_SCHEMA_ERROR)
        if package.content_checksum() != _required_checksum(
            row,
            "payload_checksum",
        ):
            raise PostgresOperationError(_CHECKSUM_ERROR)
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


PostgreSQLM7Repository = PostgresM7Repository

__all__ = ["PostgresM7Repository", "PostgreSQLM7Repository"]
