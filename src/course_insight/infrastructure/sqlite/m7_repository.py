"""SQLite persistence for M7 student-feedback recovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.tutoring import StudentFeedbackPackage
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


class SQLiteM7Repository:
    """Persist M7 feedback while keeping model audit hooks compatible."""

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
        """Keep the pre-existing optional hook as a no-op in this milestone."""

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        """Keep the pre-existing optional hook as a no-op in this milestone."""

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
        package = StudentFeedbackPackage.model_validate_json(str(row["payload"]))
        if (
            package.feedback_id != str(row["feedback_id"])
            or package.task_id != str(row["task_id"])
            or package.learner_id != str(row["learner_id"])
        ):
            raise RuntimeError("M7 feedback row identity mismatch")
        return package
