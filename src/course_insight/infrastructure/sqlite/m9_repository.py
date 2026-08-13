"""SQLite persistence for M9 analytics and teacher reviews."""

from __future__ import annotations

import sqlite3
from datetime import timezone
from pathlib import Path

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.modules.m9_teacher_analytics.repository import (
    ReviewDecisionConflictError,
)


class SQLiteM9Repository:
    """Persist M9-owned report and review recovery objects."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def insert_or_get_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str,
    ) -> TeacherAnalyticsBundle:
        if not course_id.strip():
            raise ValueError("M9 analytics course scope must not be blank")
        class_id = bundle.class_report.class_id
        learner_ids = sorted(
            report.learner_id for report in bundle.individual_reports
        )
        payload = dumps_json(bundle.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m9_teacher_analytics(
                    report_id,
                    course_id,
                    class_id,
                    generated_at,
                    learner_ids,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO NOTHING
                """,
                (
                    bundle.report_id,
                    course_id,
                    class_id,
                    bundle.generated_at.astimezone(timezone.utc).isoformat(),
                    dumps_json(learner_ids),
                    payload,
                ),
            )
            row = connection.execute(
                """
                SELECT report_id, course_id, class_id, learner_ids, payload
                FROM m9_teacher_analytics
                WHERE report_id = ?
                """,
                (bundle.report_id,),
            ).fetchone()
            stored = self._analytics_from_row(row)
            if stored != bundle or str(row["course_id"]) != course_id:
                raise RuntimeError("M9 analytics report identity conflict")
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str | None = None,
    ) -> None:
        if course_id is None:
            raise ValueError("M9 analytics persistence requires course scope")
        self.insert_or_get_analytics(bundle, course_id=course_id)

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT report_id, course_id, class_id, learner_ids, payload
                FROM m9_teacher_analytics
                WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._analytics_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT report_id, course_id, class_id, learner_ids, payload
                FROM m9_teacher_analytics
                WHERE course_id = ? AND class_id = ?
                ORDER BY generated_at DESC, rowid DESC
                """,
                (course_id, class_id),
            ).fetchall()
            for row in rows:
                bundle = self._analytics_from_row(row)
                if learner_id is None or any(
                    report.learner_id == learner_id
                    for report in bundle.individual_reports
                ):
                    return bundle.model_copy(deep=True)
            return None
        finally:
            connection.close()

    def insert_or_get_review_decision(
        self,
        decision: TeacherReviewDecision,
    ) -> TeacherReviewDecision:
        payload = dumps_json(decision.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m9_teacher_reviews(
                    decision_id,
                    audit_id,
                    expected_audit_version,
                    payload
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    decision.decision_id,
                    decision.audit_id,
                    decision.expected_audit_version,
                    payload,
                ),
            )
            row = connection.execute(
                """
                SELECT
                    decision_id,
                    audit_id,
                    expected_audit_version,
                    payload
                FROM m9_teacher_reviews
                WHERE decision_id = ?
                    OR (
                        audit_id = ?
                        AND expected_audit_version = ?
                    )
                ORDER BY CASE WHEN decision_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (
                    decision.decision_id,
                    decision.audit_id,
                    decision.expected_audit_version,
                    decision.decision_id,
                ),
            ).fetchone()
            stored = self._review_from_row(row)
            if stored != decision:
                raise ReviewDecisionConflictError(
                    "M9 teacher-review identity conflict"
                )
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_review_decision(self, decision: TeacherReviewDecision) -> None:
        self.insert_or_get_review_decision(decision)

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    decision_id,
                    audit_id,
                    expected_audit_version,
                    payload
                FROM m9_teacher_reviews
                WHERE decision_id = ?
                """,
                (decision_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._review_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    @staticmethod
    def _analytics_from_row(
        row: sqlite3.Row | None,
    ) -> TeacherAnalyticsBundle:
        if row is None:
            raise RuntimeError("M9 analytics insert produced no row")
        bundle = TeacherAnalyticsBundle.model_validate_json(str(row["payload"]))
        if (
            bundle.report_id != str(row["report_id"])
            or bundle.class_report.class_id != str(row["class_id"])
        ):
            raise RuntimeError("M9 analytics row identity mismatch")
        return bundle

    @staticmethod
    def _review_from_row(
        row: sqlite3.Row | None,
    ) -> TeacherReviewDecision:
        if row is None:
            raise RuntimeError("M9 teacher-review insert produced no row")
        decision = TeacherReviewDecision.model_validate_json(str(row["payload"]))
        if (
            decision.decision_id != str(row["decision_id"])
            or decision.audit_id != str(row["audit_id"])
            or decision.expected_audit_version
            != int(row["expected_audit_version"])
        ):
            raise RuntimeError("M9 teacher-review row identity mismatch")
        return decision
