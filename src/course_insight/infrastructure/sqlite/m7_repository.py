"""SQLite persistence for M7 feedback and privacy-minimized call audits."""

from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

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
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.modules.m7_local_model.repository import M7ModelAuditRecord


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


class SQLiteM7Repository:
    """Persist M7 feedback and privacy-minimized model-call audits."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def save_model_audit(
        self,
        audit_id: str,
        scoring_result: RubricScoringResult,
    ) -> None:
        """Retain the deprecated M8-owned score-audit compatibility hook."""

        del audit_id, scoring_result

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        """Deterministic feedback metadata remains storage-neutral."""

        del prompt_id, prompt_payload

    def save_generation_result(
        self,
        result: LLMGenerationResult,
    ) -> None:
        """Never store raw provider output."""

        del result

    def save_invocation_audit(
        self,
        audit: ModelInvocationAudit,
    ) -> None:
        """Deprecated split hook; use ``save_execution_audit`` atomically."""

        del audit

    def save_safety_check(
        self,
        result: SafetyCheckResult,
    ) -> None:
        """Deprecated split hook; use ``save_execution_audit`` atomically."""

        del result

    def save_execution_audit(self, record: M7ModelAuditRecord) -> None:
        """Atomically insert or verify one idempotent model-call audit."""

        if not isinstance(record, M7ModelAuditRecord):
            raise TypeError("record must be an M7ModelAuditRecord")
        payload = dumps_json(record.to_dict())
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.created_at.isoformat(),
                    payload,
                    checksum,
                ),
            )
            row = connection.execute(
                f"""
                SELECT {_AUDIT_COLUMNS}
                FROM m7_model_invocation_audits
                WHERE invocation_id = ? OR request_id = ?
                ORDER BY CASE WHEN invocation_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (record.invocation_id, record.request_id, record.invocation_id),
            ).fetchone()
            if self._execution_audit_from_row(row) != record:
                raise RuntimeError("M7 model audit identity conflict")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_execution_audit(
        self,
        invocation_id: str,
    ) -> M7ModelAuditRecord | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                f"""
                SELECT {_AUDIT_COLUMNS}
                FROM m7_model_invocation_audits
                WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
            return None if row is None else self._execution_audit_from_row(row)
        finally:
            connection.close()

    def purge_execution_audits_before(self, cutoff: datetime) -> int:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("M7 audit purge cutoff must be timezone-aware")
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                DELETE FROM m7_model_invocation_audits
                WHERE julianday(created_at) < julianday(?)
                RETURNING invocation_id
                """,
                (cutoff.isoformat(),),
            ).fetchall()
            connection.execute("COMMIT")
            return len(rows)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def insert_or_get_feedback(
        self,
        package: StudentFeedbackPackage,
    ) -> StudentFeedbackPackage:
        payload = dumps_json(package.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m7_student_feedback(
                    feedback_id,
                    task_id,
                    learner_id,
                    payload
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    package.feedback_id,
                    package.task_id,
                    package.learner_id,
                    payload,
                ),
            )
            row = connection.execute(
                """
                SELECT feedback_id, task_id, learner_id, payload
                FROM m7_student_feedback
                WHERE feedback_id = ?
                    OR (task_id = ? AND learner_id = ?)
                ORDER BY CASE WHEN feedback_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (
                    package.feedback_id,
                    package.task_id,
                    package.learner_id,
                    package.feedback_id,
                ),
            ).fetchone()
            stored = self._feedback_from_row(row)
            if stored != package:
                raise RuntimeError("M7 feedback identity conflict")
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_feedback(self, package: StudentFeedbackPackage) -> None:
        self.insert_or_get_feedback(package)

    def get_feedback(
        self,
        feedback_id: str,
    ) -> StudentFeedbackPackage | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT feedback_id, task_id, learner_id, payload
                FROM m7_student_feedback
                WHERE feedback_id = ?
                """,
                (feedback_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._feedback_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_feedback_for_task(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT feedback_id, task_id, learner_id, payload
                FROM m7_student_feedback
                WHERE task_id = ? AND learner_id = ?
                """,
                (task_id, learner_id),
            ).fetchone()
            return (
                None
                if row is None
                else self._feedback_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_feedback_by_task_and_learner(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        return self.get_feedback_for_task(task_id, learner_id)

    @staticmethod
    def _feedback_from_row(
        row: sqlite3.Row | None,
    ) -> StudentFeedbackPackage:
        if row is None:
            raise RuntimeError("M7 feedback insert produced no row")
        try:
            payload = json.loads(str(row["payload"]))
            if type(payload) is not dict:
                raise ValueError
            _migrate_legacy_quotes(payload)
            package = StudentFeedbackPackage.model_validate(payload)
        except Exception:
            raise RuntimeError("M7 feedback payload is invalid") from None
        if (
            package.feedback_id != str(row["feedback_id"])
            or package.task_id != str(row["task_id"])
            or package.learner_id != str(row["learner_id"])
        ):
            raise RuntimeError("M7 feedback row identity mismatch")
        return package

    @staticmethod
    def _execution_audit_from_row(
        row: sqlite3.Row | None,
    ) -> M7ModelAuditRecord:
        if row is None:
            raise RuntimeError("M7 model audit insert produced no row")
        payload_text = str(row["payload"])
        expected_checksum = hashlib.sha256(
            payload_text.encode("utf-8")
        ).hexdigest()
        stored_checksum = str(row["payload_checksum"])
        if not hmac.compare_digest(expected_checksum, stored_checksum):
            raise RuntimeError("M7 model audit payload checksum mismatch")
        try:
            payload = json.loads(payload_text)
            record = M7ModelAuditRecord.from_dict(payload)
            if (
                record.invocation_id != str(row["invocation_id"])
                or record.request_id != str(row["request_id"])
                or record.scoring_task_id != str(row["scoring_task_id"])
                or record.provider != str(row["provider"])
                or record.model_name != str(row["model_name"])
                or record.provider_status != str(row["provider_status"])
                or record.validation_status != str(row["validation_status"])
                or record.privacy_decision != str(row["privacy_decision"])
                or record.created_at.isoformat() != str(row["created_at"])
            ):
                raise ValueError
            return record
        except Exception:
            raise RuntimeError("M7 model audit integrity check failed") from None


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


__all__ = ["SQLiteM7Repository"]
