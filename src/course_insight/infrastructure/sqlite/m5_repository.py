"""SQLite persistence for complete M5 state-update histories."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


class SQLiteM5Repository:
    """Persist state results within M5-owned tables."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def insert_or_get_state_update(
        self,
        result: StateUpdateResult,
    ) -> StateUpdateResult:
        """Insert an attempt result once or return its identical winner."""

        learner = result.learner_state_snapshot
        class_state = result.class_state_snapshot
        attempt_id = result.diagnosis_result.attempt_id
        result.assert_consistent()
        payload = dumps_json(result.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_or_validate_learner(connection, learner)
            self._insert_or_validate_class(connection, class_state)
            connection.execute(
                """
                INSERT INTO m5_state_updates(
                    attempt_id,
                    course_id,
                    class_id,
                    learner_id,
                    state_version,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id, state_version) DO NOTHING
                """,
                (
                    attempt_id,
                    learner.course_id,
                    learner.class_id,
                    learner.learner_id,
                    learner.state_version,
                    payload,
                ),
            )
            row = connection.execute(
                """
                SELECT
                    attempt_id,
                    course_id,
                    class_id,
                    learner_id,
                    state_version,
                    payload
                FROM m5_state_updates
                WHERE attempt_id = ? AND state_version = ?
                """,
                (attempt_id, learner.state_version),
            ).fetchone()
            stored = self._state_result_from_row(row)
            if stored != result:
                raise RuntimeError(
                    "M5 state-update conflict for the same attempt version"
                )
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_state_update(self, result: StateUpdateResult) -> None:
        self.insert_or_get_state_update(result)

    def get_state_update(self, attempt_id: str) -> StateUpdateResult | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    attempt_id,
                    course_id,
                    class_id,
                    learner_id,
                    state_version,
                    payload
                FROM m5_state_updates
                WHERE attempt_id = ?
                ORDER BY state_version DESC
                LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._state_result_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_state_update_result(
        self,
        attempt_id: str,
    ) -> StateUpdateResult | None:
        return self.get_state_update(attempt_id)

    def get_state_update_version(
        self,
        attempt_id: str,
        state_version: int,
    ) -> StateUpdateResult | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    attempt_id,
                    course_id,
                    class_id,
                    learner_id,
                    state_version,
                    payload
                FROM m5_state_updates
                WHERE attempt_id = ? AND state_version = ?
                """,
                (attempt_id, state_version),
            ).fetchone()
            return (
                None
                if row is None
                else self._state_result_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_state_update_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> StateUpdateResult | None:
        key = f"{audit_id}:{audit_version}"
        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT
                    attempt_id,
                    course_id,
                    class_id,
                    learner_id,
                    state_version,
                    payload
                FROM m5_state_updates
                WHERE attempt_id = ?
                ORDER BY state_version ASC
                """,
                (attempt_id,),
            ).fetchall()
            for row in rows:
                result = self._state_result_from_row(row)
                if key in result.processed_audit_ids:
                    return result.model_copy(deep=True)
            return None
        finally:
            connection.close()

    def save_learner_state(self, snapshot: LearnerStateSnapshot) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_or_validate_learner(connection, snapshot)
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
        """Retain the old lookup and fail closed if its scope is ambiguous."""

        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT snapshot_id, course_id, class_id, learner_id, payload
                FROM m5_learner_states
                WHERE learner_id = ? AND state_version = ?
                """,
                (learner_id, state_version),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError(
                    "M5 legacy learner-state lookup is ambiguous across courses"
                )
            return (
                None
                if not rows
                else self._learner_from_row(rows[0]).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Load one learner version under its complete persisted scope."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT snapshot_id, course_id, class_id, learner_id, payload
                FROM m5_learner_states
                WHERE course_id = ? AND class_id = ?
                    AND learner_id = ? AND state_version = ?
                """,
                (course_id, class_id, learner_id, state_version),
            ).fetchone()
            if row is None:
                return None
            snapshot = self._learner_from_row(row)
            if (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.learner_id,
                snapshot.state_version,
            ) != (course_id, class_id, learner_id, state_version):
                raise RuntimeError("M5 learner-state scope mismatch")
            return snapshot.model_copy(deep=True)
        finally:
            connection.close()

    def get_latest_learner_state(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT snapshot_id, course_id, class_id, learner_id, payload
                FROM m5_learner_states
                WHERE course_id = ? AND class_id = ? AND learner_id = ?
                ORDER BY state_version DESC
                LIMIT 1
                """,
                (course_id, class_id, learner_id),
            ).fetchone()
            return (
                None
                if row is None
                else self._learner_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_or_validate_class(connection, snapshot)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT snapshot_id, course_id, class_id, payload
                FROM m5_class_states
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchall()
            if len(rows) > 1:
                raise RuntimeError(
                    "M5 legacy class-state lookup is ambiguous across courses"
                )
            return None if not rows else self._class_from_row(
                rows[0]
            ).model_copy(deep=True)
        finally:
            connection.close()

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ) -> ClassStateSnapshot | None:
        """Load one class identity without crossing a teaching scope."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT snapshot_id, course_id, class_id, payload
                FROM m5_class_states
                WHERE course_id = ? AND class_id = ? AND snapshot_id = ?
                """,
                (course_id, class_id, snapshot_id),
            ).fetchone()
            if row is None:
                return None
            snapshot = self._class_from_row(row)
            if (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.snapshot_id,
            ) != (course_id, class_id, snapshot_id):
                raise RuntimeError("M5 class-state identity mismatch")
            return snapshot.model_copy(deep=True)
        finally:
            connection.close()

    def get_class_state_exact(
        self,
        course_id: str,
        class_id: str,
        state_version: int,
    ) -> ClassStateSnapshot | None:
        """Load one class aggregate under its internal numeric version."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    snapshot_id,
                    course_id,
                    class_id,
                    state_version,
                    payload
                FROM m5_class_states
                WHERE course_id = ? AND class_id = ? AND state_version = ?
                """,
                (course_id, class_id, state_version),
            ).fetchone()
            if row is None:
                return None
            snapshot = self._class_from_row(row)
            stored_version = row["state_version"]
            if (
                snapshot.course_id,
                snapshot.class_id,
            ) != (course_id, class_id) or (
                type(stored_version) is not int
                or stored_version != state_version
            ):
                raise RuntimeError("M5 class-state scope mismatch")
            return snapshot.model_copy(deep=True)
        finally:
            connection.close()

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT snapshot_id, course_id, class_id, payload
                FROM m5_class_states
                WHERE course_id = ? AND class_id = ?
                ORDER BY state_version DESC
                LIMIT 1
                """,
                (course_id, class_id),
            ).fetchone()
            return (
                None
                if row is None
                else self._class_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_processed_audit_ids(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> frozenset[str]:
        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT
                    attempt_id,
                    course_id,
                    class_id,
                    learner_id,
                    state_version,
                    payload
                FROM m5_state_updates
                WHERE course_id = ? AND class_id = ? AND learner_id = ?
                """,
                (course_id, class_id, learner_id),
            ).fetchall()
            return frozenset(
                audit_id
                for row in rows
                for audit_id in self._state_result_from_row(
                    row
                ).processed_audit_ids
            )
        finally:
            connection.close()

    @staticmethod
    def _insert_or_validate_learner(
        connection: sqlite3.Connection,
        snapshot: LearnerStateSnapshot,
    ) -> None:
        payload = dumps_json(snapshot.to_dict())
        connection.execute(
            """
            INSERT INTO m5_learner_states(
                snapshot_id,
                course_id,
                class_id,
                learner_id,
                state_version,
                payload
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(course_id, class_id, learner_id, state_version)
            DO NOTHING
            """,
            (
                snapshot.snapshot_id,
                snapshot.course_id,
                snapshot.class_id,
                snapshot.learner_id,
                snapshot.state_version,
                payload,
            ),
        )
        row = connection.execute(
            """
            SELECT snapshot_id, course_id, class_id, learner_id, payload
            FROM m5_learner_states
            WHERE course_id = ? AND class_id = ?
                AND learner_id = ? AND state_version = ?
            """,
            (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.learner_id,
                snapshot.state_version,
            ),
        ).fetchone()
        if SQLiteM5Repository._learner_from_row(row) != snapshot:
            raise RuntimeError("M5 learner-state version conflict")

    @staticmethod
    def _insert_or_validate_class(
        connection: sqlite3.Connection,
        snapshot: ClassStateSnapshot,
    ) -> None:
        payload = dumps_json(snapshot.to_dict())
        existing = connection.execute(
            """
            SELECT snapshot_id, course_id, class_id, payload
            FROM m5_class_states
            WHERE course_id = ? AND class_id = ? AND snapshot_id = ?
            """,
            (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.snapshot_id,
            ),
        ).fetchone()
        if existing is not None:
            if SQLiteM5Repository._class_from_row(existing) != snapshot:
                raise RuntimeError("M5 class-state identity conflict")
            return
        next_version = connection.execute(
            """
            SELECT COALESCE(MAX(state_version), 0) + 1
            FROM m5_class_states
            WHERE course_id = ? AND class_id = ?
            """,
            (snapshot.course_id, snapshot.class_id),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO m5_class_states(
                snapshot_id,
                course_id,
                class_id,
                state_version,
                aggregation_policy_version,
                payload
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(course_id, class_id, snapshot_id) DO NOTHING
            """,
            (
                snapshot.snapshot_id,
                snapshot.course_id,
                snapshot.class_id,
                next_version,
                snapshot.aggregation_policy_version,
                payload,
            ),
        )
        row = connection.execute(
            """
            SELECT snapshot_id, course_id, class_id, payload
            FROM m5_class_states
            WHERE course_id = ? AND class_id = ? AND snapshot_id = ?
            """,
            (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.snapshot_id,
            ),
        ).fetchone()
        if SQLiteM5Repository._class_from_row(row) != snapshot:
            raise RuntimeError("M5 class-state identity conflict")

    @staticmethod
    def _learner_from_row(row: sqlite3.Row | None) -> LearnerStateSnapshot:
        if row is None:
            raise RuntimeError("M5 learner-state insert produced no row")
        snapshot = LearnerStateSnapshot.model_validate_json(str(row["payload"]))
        if (
            snapshot.snapshot_id != str(row["snapshot_id"])
            or snapshot.course_id != str(row["course_id"])
            or snapshot.class_id != str(row["class_id"])
            or snapshot.learner_id != str(row["learner_id"])
        ):
            raise RuntimeError("M5 learner-state row identity mismatch")
        return snapshot

    @staticmethod
    def _class_from_row(row: sqlite3.Row | None) -> ClassStateSnapshot:
        if row is None:
            raise RuntimeError("M5 class-state insert produced no row")
        snapshot = ClassStateSnapshot.model_validate_json(str(row["payload"]))
        if (
            snapshot.snapshot_id != str(row["snapshot_id"])
            or snapshot.course_id != str(row["course_id"])
            or snapshot.class_id != str(row["class_id"])
        ):
            raise RuntimeError("M5 class-state row identity mismatch")
        return snapshot

    @staticmethod
    def _state_result_from_row(row: sqlite3.Row | None) -> StateUpdateResult:
        if row is None:
            raise RuntimeError("M5 state-update insert produced no row")
        result = StateUpdateResult.model_validate_json(str(row["payload"]))
        learner = result.learner_state_snapshot
        if (
            result.diagnosis_result.attempt_id != str(row["attempt_id"])
            or learner.state_version != int(row["state_version"])
            or learner.course_id != str(row["course_id"])
            or learner.class_id != str(row["class_id"])
            or learner.learner_id != str(row["learner_id"])
        ):
            raise RuntimeError("M5 state-update row identity mismatch")
        return result
