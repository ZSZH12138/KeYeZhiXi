"""SQLite v2 legacy and v7 leased-outbox schema boundaries."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from course_insight.modules.m0_platform.outbox import (
    utc_text,
    validate_serialized_record,
)


LEARNING_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS m0_learning_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    event_type TEXT NOT NULL CHECK (length(event_type) > 0),
    occurred_at TEXT NOT NULL CHECK (length(occurred_at) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    )
)
"""
LEGACY_OUTBOX_SQL = """
CREATE TABLE IF NOT EXISTS m0_event_outbox (
    event_id TEXT PRIMARY KEY
        REFERENCES m0_learning_events(event_id) ON DELETE CASCADE,
    record TEXT NOT NULL CHECK (
        CASE WHEN json_valid(record)
            THEN json(record) = record
            ELSE 0
        END
    )
)
"""
OUTBOX_V7_SQL = """
CREATE TABLE IF NOT EXISTS m0_event_outbox (
    event_id TEXT PRIMARY KEY
        REFERENCES m0_learning_events(event_id) ON DELETE CASCADE
        CHECK (length(event_id) > 0),
    record TEXT NOT NULL CHECK (
        CASE WHEN json_valid(record)
            THEN json(record) = record
            ELSE 0
        END
    ),
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'dead')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    version INTEGER NOT NULL CHECK (version >= 1),
    available_at TEXT NOT NULL CHECK (
        julianday(available_at) IS NOT NULL
        AND substr(available_at, -6) = '+00:00'
    ),
    locked_by TEXT CHECK (
        locked_by IS NULL OR (
            length(locked_by) BETWEEN 1 AND 128
            AND locked_by NOT GLOB '*[^A-Za-z0-9_.:@-]*'
        )
    ),
    locked_at TEXT CHECK (
        locked_at IS NULL OR (
            julianday(locked_at) IS NOT NULL
            AND substr(locked_at, -6) = '+00:00'
        )
    ),
    lease_until TEXT CHECK (
        lease_until IS NULL OR (
            julianday(lease_until) IS NOT NULL
            AND substr(lease_until, -6) = '+00:00'
        )
    ),
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR (
            length(last_error_code) BETWEEN 1 AND 128
            AND last_error_code GLOB '[A-Z]*'
            AND last_error_code NOT GLOB '*[^A-Z0-9_]*'
        )
    ),
    created_at TEXT NOT NULL CHECK (
        julianday(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
    ),
    updated_at TEXT NOT NULL CHECK (
        julianday(updated_at) IS NOT NULL
        AND substr(updated_at, -6) = '+00:00'
        AND julianday(updated_at) >= julianday(created_at)
    ),
    CHECK (
        (
            status = 'processing'
            AND attempt_count >= 1
            AND locked_by IS NOT NULL
            AND locked_at IS NOT NULL
            AND lease_until IS NOT NULL
            AND julianday(lease_until) > julianday(locked_at)
        )
        OR (
            status IN ('pending', 'dead')
            AND locked_by IS NULL
            AND locked_at IS NULL
            AND lease_until IS NULL
        )
    )
)
"""
OUTBOX_CLAIM_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS m0_outbox_claim_order
ON m0_event_outbox(status, available_at, created_at, event_id)
"""
OUTBOX_V7_MIGRATION_NAME = "m0_leased_event_outbox"


def migrate_outbox_v6_to_v7(connection: sqlite3.Connection) -> None:
    """Replace the legacy table without losing valid pending rows."""

    validate_outbox_schema(connection, schema_version=6)
    migrated_at = utc_text(datetime.now(timezone.utc))
    rows = connection.execute(
        """
        SELECT outbox.event_id, outbox.record, events.occurred_at
        FROM m0_event_outbox AS outbox
        JOIN m0_learning_events AS events USING (event_id)
        ORDER BY outbox.event_id
        """
    ).fetchall()
    migrated: list[tuple[str, str]] = []
    for row in rows:
        event_id = str(row["event_id"])
        record = str(row["record"])
        validate_serialized_record(event_id, record)
        _normalize_legacy_timestamp(str(row["occurred_at"]))
        migrated.append(
            (
                event_id,
                record,
            )
        )

    connection.execute(
        "ALTER TABLE m0_event_outbox RENAME TO m0_event_outbox_v6"
    )
    connection.execute(OUTBOX_V7_SQL)
    connection.execute(OUTBOX_CLAIM_INDEX_SQL)
    connection.executemany(
        """
        INSERT INTO m0_event_outbox(
            event_id,
            record,
            status,
            attempt_count,
            version,
            available_at,
            locked_by,
            locked_at,
            lease_until,
            last_error_code,
            created_at,
            updated_at
        ) VALUES (?, ?, 'pending', 0, 1, ?, NULL, NULL, NULL, NULL, ?, ?)
        """,
        [
            (event_id, record, migrated_at, migrated_at, migrated_at)
            for event_id, record in migrated
        ],
    )
    connection.execute("DROP TABLE m0_event_outbox_v6")
    validate_outbox_schema(connection, schema_version=7)


def validate_outbox_schema(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    expected = LEGACY_OUTBOX_SQL if schema_version < 7 else OUTBOX_V7_SQL
    if _table_schema(connection, "m0_event_outbox") != _normalize_sql(expected):
        raise RuntimeError("M0 schema is incompatible")
    if connection.execute(
        "PRAGMA foreign_key_check('m0_event_outbox')"
    ).fetchone() is not None:
        raise RuntimeError("M0 outbox data violates foreign keys")
    if schema_version < 7:
        return
    index_row = connection.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type = 'index' AND name = 'm0_outbox_claim_order'
        """
    ).fetchone()
    if (
        index_row is None
        or index_row[0] is None
        or _normalize_sql(str(index_row[0]))
        != _normalize_sql(OUTBOX_CLAIM_INDEX_SQL)
    ):
        raise RuntimeError("M0 outbox claim index is incompatible")


def validate_m0_storage_schema(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    if _table_schema(
        connection,
        "m0_learning_events",
    ) != _normalize_sql(LEARNING_EVENTS_SQL):
        raise RuntimeError("M0 schema is incompatible")
    validate_outbox_schema(connection, schema_version=schema_version)


def _normalize_legacy_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise RuntimeError("legacy M0 event timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("legacy M0 event timestamp is not timezone-aware")
    return utc_text(parsed.astimezone(timezone.utc))


def _table_schema(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return _normalize_sql("" if row is None or row[0] is None else str(row[0]))


def _normalize_sql(value: str) -> str:
    normalized = "".join(
        character.casefold()
        for character in value
        if not character.isspace()
    ).removesuffix(";")
    optional_table = "createtableifnotexists"
    optional_index = "createindexifnotexists"
    if normalized.startswith(optional_table):
        return f"createtable{normalized[len(optional_table):]}"
    if normalized.startswith(optional_index):
        return f"createindex{normalized[len(optional_index):]}"
    return normalized


__all__ = [
    "LEARNING_EVENTS_SQL",
    "LEGACY_OUTBOX_SQL",
    "OUTBOX_CLAIM_INDEX_SQL",
    "OUTBOX_V7_MIGRATION_NAME",
    "OUTBOX_V7_SQL",
    "migrate_outbox_v6_to_v7",
    "validate_outbox_schema",
    "validate_m0_storage_schema",
]
