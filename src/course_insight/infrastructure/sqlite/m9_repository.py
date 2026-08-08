"""SQLite persistence for M9 analytics and teacher reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
    M9ReviewDecisionConflict,
)
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


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

    def save_model_audit(self, record: M9ModelAuditRecord) -> None:
        """Persist one idempotent privacy-minimized model-call record."""

        if not isinstance(record, M9ModelAuditRecord):
            raise TypeError("record must be an M9ModelAuditRecord")
        payload = dumps_json(record.to_dict())
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            authoritative_source_checksum = self._source_report_checksum(
                connection,
                record.source_report_id,
            )
            if not hmac.compare_digest(
                record.source_report_checksum,
                authoritative_source_checksum,
            ):
                raise RuntimeError("M9 model audit source report checksum mismatch")
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.created_at.astimezone(timezone.utc).isoformat(),
                    payload,
                    checksum,
                ),
            )
            row = connection.execute(
                """
                SELECT
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
                FROM m9_model_invocation_audits
                WHERE invocation_id = ? OR request_id = ?
                ORDER BY CASE WHEN invocation_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (record.invocation_id, record.request_id, record.invocation_id),
            ).fetchone()
            if self._model_audit_from_row(
                row,
                authoritative_source_checksum=authoritative_source_checksum,
            ) != record:
                raise RuntimeError("M9 model audit identity conflict")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_model_audit(
        self,
        invocation_id: str,
    ) -> M9ModelAuditRecord | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
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
                FROM m9_model_invocation_audits
                WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
            if row is None:
                return None
            authoritative_source_checksum = self._source_report_checksum(
                connection,
                str(row["source_report_id"]),
            )
            return self._model_audit_from_row(
                row,
                authoritative_source_checksum=authoritative_source_checksum,
            )
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

    def get_scoped_analytics(
        self,
        report_id: str,
        *,
        course_id: str,
        class_id: str,
    ) -> TeacherAnalyticsBundle | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT report_id, course_id, class_id, learner_ids, payload
                FROM m9_teacher_analytics
                WHERE report_id = ? AND course_id = ? AND class_id = ?
                """,
                (report_id, course_id, class_id),
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
                ORDER BY generated_at DESC, report_id DESC
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
                raise M9ReviewDecisionConflict(
                    audit_id=decision.audit_id,
                    expected_audit_version=(
                        decision.expected_audit_version
                    ),
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

    @staticmethod
    def _source_report_checksum(
        connection: sqlite3.Connection,
        source_report_id: str,
    ) -> str:
        row = connection.execute(
            "SELECT payload FROM m9_teacher_analytics WHERE report_id = ?",
            (source_report_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("M9 model audit source report is unavailable")
        bundle = TeacherAnalyticsBundle.model_validate_json(str(row["payload"]))
        if bundle.report_id != source_report_id:
            raise RuntimeError("M9 model audit source report identity mismatch")
        return bundle.content_checksum()

    @staticmethod
    def _model_audit_from_row(
        row: sqlite3.Row | None,
        *,
        authoritative_source_checksum: str,
    ) -> M9ModelAuditRecord:
        if row is None:
            raise RuntimeError("M9 model audit insert produced no row")
        payload = str(row["payload"])
        expected_checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(
            expected_checksum,
            str(row["payload_checksum"]),
        ):
            raise RuntimeError("M9 model audit payload checksum mismatch")
        record = M9ModelAuditRecord.from_dict(
            json.loads(payload)
        )
        stored_source_checksum = str(row["source_report_checksum"])
        if (
            record.invocation_id != str(row["invocation_id"])
            or record.request_id != str(row["request_id"])
            or record.source_report_id != str(row["source_report_id"])
            or not hmac.compare_digest(
                record.source_report_checksum,
                stored_source_checksum,
            )
            or not hmac.compare_digest(
                stored_source_checksum,
                authoritative_source_checksum,
            )
            or record.scope != str(row["scope"])
            or record.provider != str(row["provider"])
            or record.model_name != str(row["model_name"])
            or record.provider_status != str(row["provider_status"])
            or record.validation_status != str(row["validation_status"])
            or record.created_at
            != datetime.fromisoformat(str(row["created_at"]))
        ):
            raise RuntimeError("M9 model audit row identity mismatch")
        return record
