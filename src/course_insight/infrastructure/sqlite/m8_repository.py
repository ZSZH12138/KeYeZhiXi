"""SQLite implementation of the M8 assessment-and-scoring persistence boundary."""

from __future__ import annotations

from pathlib import Path

from course_insight.contracts.assessment import AssessmentPaper, ScoreAuditRecord
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


class SQLiteM8Repository:
    """Persist replay-safe M8 papers and audits without leaking SQLite into M8 services."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        """Create the database and apply all pending migrations."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    # ── papers ──

    def save_paper(self, paper: AssessmentPaper) -> None:
        """Persist one immutable generated assessment paper."""

        payload = dumps_json(paper.model_dump(mode="json"))
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m8_papers (paper_id, payload)
                VALUES (?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET payload = excluded.payload
                """,
                (paper.paper_id, payload),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_paper(self, paper_id: str) -> AssessmentPaper | None:
        """Load one frozen assessment paper by its identity."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                "SELECT payload FROM m8_papers WHERE paper_id = ?",
                (paper_id,),
            ).fetchone()
            return None if row is None else AssessmentPaper.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()

    # ── score audits ──

    def save_score_audit(self, record: ScoreAuditRecord) -> None:
        """Append one exact score-audit version."""

        payload = dumps_json(record.model_dump(mode="json"))
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m8_score_audits
                    (audit_id, audit_version, item_instance_id, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(audit_id, audit_version) DO UPDATE SET
                    item_instance_id = excluded.item_instance_id,
                    payload = excluded.payload
                """,
                (
                    record.audit_id,
                    record.audit_version,
                    record.item_instance_id,
                    payload,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_score_audit(
        self,
        audit_id: str,
        audit_version: int,
    ) -> ScoreAuditRecord | None:
        """Load one exact score-audit version."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT payload FROM m8_score_audits
                WHERE audit_id = ? AND audit_version = ?
                """,
                (audit_id, audit_version),
            ).fetchone()
            return None if row is None else ScoreAuditRecord.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()

    def get_latest_score_audit(self, audit_id: str) -> ScoreAuditRecord | None:
        """Load the highest-version score audit for concurrent-version detection."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT payload FROM m8_score_audits
                WHERE audit_id = ?
                ORDER BY audit_version DESC
                LIMIT 1
                """,
                (audit_id,),
            ).fetchone()
            return None if row is None else ScoreAuditRecord.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()
