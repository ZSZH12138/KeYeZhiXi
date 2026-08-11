"""SQLite implementation of the M5 learner-and-class-state persistence boundary."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


class SQLiteM5Repository:
    """Persist replay-safe M5 state snapshots without leaking SQLite into M5 services."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        """Create the database and apply all pending migrations."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    # ── learner state ──

    def save_learner_state(self, snapshot: LearnerStateSnapshot) -> None:
        """Persist one versioned learner-state snapshot.

        D-09 修复：同 (learner_id, state_version) 冲突时不覆盖 payload，
        让 IntegrityError 自然抛出，而非静默 DO UPDATE。
        """

        payload = dumps_json(snapshot.model_dump(mode="json"))
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m5_learner_states
                    (snapshot_id, learner_id, state_version, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.learner_id,
                    snapshot.state_version,
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

    def get_learner_state(
        self,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Load one exact learner-state version."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT payload FROM m5_learner_states
                WHERE learner_id = ? AND state_version = ?
                """,
                (learner_id, state_version),
            ).fetchone()
            return None if row is None else LearnerStateSnapshot.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()

    def get_latest_learner_state(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        """Load the highest-version learner-state snapshot for restart recovery.

        D-06 修复：按 course+class+learner 作用域查询，而非仅按 learner_id。
        """

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT payload FROM m5_learner_states
                WHERE learner_id = ?
                  AND json_extract(payload, '$.course_id') = ?
                  AND json_extract(payload, '$.class_id') = ?
                ORDER BY state_version DESC
                LIMIT 1
                """,
                (learner_id, course_id, class_id),
            ).fetchone()
            return None if row is None else LearnerStateSnapshot.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()

    # ── class state ──

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        """Persist one class-state aggregate."""

        payload = dumps_json(snapshot.model_dump(mode="json"))
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m5_class_states
                    (snapshot_id, class_id, aggregation_policy_version, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.class_id,
                    snapshot.aggregation_policy_version,
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

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        """Load one class-state aggregate by snapshot identity."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                "SELECT payload FROM m5_class_states WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            return None if row is None else ClassStateSnapshot.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        """Load the most recent class-state aggregate for restart recovery.

        D-05 修复：按 course+class 作用域查询，而非仅按 class_id。
        """

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT payload FROM m5_class_states
                WHERE class_id = ?
                  AND json_extract(payload, '$.course_id') = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (class_id, course_id),
            ).fetchone()
            return None if row is None else ClassStateSnapshot.model_validate_json(
                str(row["payload"])
            )
        finally:
            connection.close()

    # ── processed audits watermark ──

    def get_processed_audits(self, learner_id: str) -> frozenset[str]:
        """Load the set of audit keys already processed for one learner."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                "SELECT audit_keys FROM m5_processed_audits WHERE learner_id = ?",
                (learner_id,),
            ).fetchone()
            if row is None:
                return frozenset()
            keys = json.loads(str(row["audit_keys"]))
            return frozenset(str(k) for k in keys)
        finally:
            connection.close()

    def save_processed_audits(
        self,
        learner_id: str,
        audit_keys: frozenset[str],
    ) -> None:
        """Persist the full set of processed audit keys for one learner."""

        keys_json = dumps_json(sorted(audit_keys))
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m5_processed_audits (learner_id, audit_keys)
                VALUES (?, ?)
                ON CONFLICT(learner_id) DO UPDATE SET audit_keys = excluded.audit_keys
                """,
                (learner_id, keys_json),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
