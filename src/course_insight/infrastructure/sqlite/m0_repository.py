"""SQLite implementation of the M0 persistence boundary."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
    validate_m0_schema,
)


class SQLiteM0Repository:
    """Persist M0 records without leaking SQLite into the service layer."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        """Create the database and apply all pending migrations."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def append_events(
        self,
        events: Sequence[LearningEvent],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Insert event rows and outbox rows in one explicit transaction."""

        accepted_event_ids: list[str] = []
        duplicate_event_ids: list[str] = []
        seen_input: set[str] = set()
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for event in events:
                event_id = event.idempotency_key()
                if event_id in seen_input:
                    continue
                seen_input.add(event_id)
                exists = connection.execute(
                    "SELECT 1 FROM m0_learning_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if exists is not None:
                    duplicate_event_ids.append(event_id)
                    continue
                connection.execute(
                    """
                    INSERT INTO m0_learning_events(
                        event_id, idempotency_key, event_type, occurred_at, payload
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event_id,
                        event.event_type,
                        event.occurred_at.isoformat(),
                        dumps_json(event.payload),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO m0_event_outbox(event_id, record)
                    VALUES (?, ?)
                    """,
                    (event.event_id, dumps_json(event.to_dict())),
                )
                accepted_event_ids.append(event.event_id)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return tuple(accepted_event_ids), tuple(duplicate_event_ids)

    def deliver_outbox_records(
        self,
        deliver: Callable[[str, str], None],
    ) -> None:
        """Serialize delivery and deletion under one immediate transaction."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT event_id, record FROM m0_event_outbox ORDER BY event_id"
            ).fetchall()
            for row in rows:
                event_id = str(row["event_id"])
                deliver(event_id, str(row["record"]))
                connection.execute(
                    "DELETE FROM m0_event_outbox WHERE event_id = ?",
                    (event_id,),
                )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def schema_is_current(self) -> bool:
        """Check the migration version and M0 storage structure."""

        connection = connect_sqlite(self._database_path)
        try:
            if current_schema_version(connection) != SCHEMA_VERSION:
                return False
            try:
                validate_m0_schema(connection)
            except RuntimeError:
                return False
            return True
        finally:
            connection.close()
