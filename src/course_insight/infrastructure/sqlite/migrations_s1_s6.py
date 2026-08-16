"""Versioned SQLite schema owned by the S1-S6 repository boundary.

The existing SQLite migration ledger is intentionally left at version 13 for
backward compatibility with the M0-M9 platform.  S1-S6 owns a separate small
ledger so this slice can be deployed independently and rolled back without
changing PostgreSQL or the historical platform migration contract.
"""

from __future__ import annotations

import sqlite3


S1_S6_SCHEMA_VERSION = 1
S1_S6_MIGRATION_NAME = "s1_s6_repository_records"
_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS s1_s6_schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE CHECK (length(name) > 0)
)
"""
_RECORDS_SQL = """
CREATE TABLE IF NOT EXISTS s1_s6_artifacts (
    module TEXT NOT NULL CHECK (module IN ('m1', 'm2', 'm3')),
    object_type TEXT NOT NULL CHECK (length(object_type) > 0),
    object_id TEXT NOT NULL CHECK (length(object_id) > 0),
    object_version TEXT NOT NULL CHECK (length(object_version) > 0),
    status TEXT NOT NULL CHECK (length(status) > 0),
    content_checksum TEXT NOT NULL CHECK (
        length(content_checksum) = 64
        AND content_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    payload_version TEXT NOT NULL CHECK (length(payload_version) > 0),
    payload_checksum TEXT NOT NULL CHECK (
        length(payload_checksum) = 64
        AND payload_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    created_at TEXT NOT NULL CHECK (datetime(created_at) IS NOT NULL),
    PRIMARY KEY (module, object_type, object_id, object_version)
)
"""


def _normalize_sql(value: str) -> str:
    normalized = "".join(value.casefold().split()).removesuffix(";")
    return normalized.replace("createtableifnotexists", "createtable", 1)


def current_schema_version(connection_or_path: sqlite3.Connection | str) -> int:
    """Return the S1-S6 ledger version, or zero before initialization."""

    if isinstance(connection_or_path, sqlite3.Connection):
        connection = connection_or_path
        close = False
    else:
        connection = sqlite3.connect(str(connection_or_path))
        close = True
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 's1_s6_schema_migrations'"
        ).fetchone()
        if table is None:
            return 0
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) "
                "FROM s1_s6_schema_migrations"
            ).fetchone()[0]
        )
    finally:
        if close:
            connection.close()


def _validate_records_schema(connection: sqlite3.Connection) -> None:
    columns = [
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('s1_s6_artifacts')"
        ).fetchall()
    ]
    expected = [
        "module",
        "object_type",
        "object_id",
        "object_version",
        "status",
        "content_checksum",
        "payload_version",
        "payload_checksum",
        "payload",
        "created_at",
    ]
    if columns != expected:
        raise RuntimeError("S1-S6 SQLite artifact schema is incompatible")
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 's1_s6_artifacts'"
    ).fetchone()
    if row is None or _normalize_sql(str(row[0])) != _normalize_sql(_RECORDS_SQL):
        raise RuntimeError("S1-S6 SQLite artifact constraints are incompatible")


def _validate_ledger(connection: sqlite3.Connection) -> None:
    rows = [
        (int(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT version, name FROM s1_s6_schema_migrations "
            "ORDER BY version"
        ).fetchall()
    ]
    if rows != [(1, S1_S6_MIGRATION_NAME)]:
        raise RuntimeError("S1-S6 SQLite migration ledger is incompatible")


def validate_schema(connection: sqlite3.Connection) -> None:
    """Validate the independent S1-S6 ledger and artifact table."""

    if current_schema_version(connection) != S1_S6_SCHEMA_VERSION:
        raise RuntimeError("S1-S6 SQLite schema version is not current")
    _validate_ledger(connection)
    _validate_records_schema(connection)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Apply the S1-S6 migration inside the caller's transaction."""

    connection.execute(_LEDGER_SQL)
    version = current_schema_version(connection)
    if version > S1_S6_SCHEMA_VERSION:
        raise RuntimeError("S1-S6 SQLite schema is newer than this application")
    applied = {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM s1_s6_schema_migrations"
        ).fetchall()
    }
    if 1 not in applied:
        connection.execute(_RECORDS_SQL)
        connection.execute(
            "INSERT INTO s1_s6_schema_migrations(version, name) VALUES (?, ?)",
            (1, S1_S6_MIGRATION_NAME),
        )
    validate_schema(connection)


def migrate(connection: sqlite3.Connection) -> None:
    """Apply S1-S6 migrations in an explicit transaction."""

    if connection.in_transaction:
        raise RuntimeError("S1-S6 migrations require an idle SQLite connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        ensure_schema(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


__all__ = [
    "S1_S6_MIGRATION_NAME",
    "S1_S6_SCHEMA_VERSION",
    "current_schema_version",
    "ensure_schema",
    "migrate",
    "validate_schema",
]
