"""Deterministic explicit-transaction SQLite migrations."""

from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 3
_INITIAL_MIGRATION_NAME = "initial_module_tables"
_OUTBOX_MIGRATION_NAME = "m0_event_outbox"
_M6_DECISION_MIGRATION_NAME = "m6_tutoring_decisions"
_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE CHECK (length(name) > 0)
)
"""
_INITIAL_TABLE_STATEMENTS = (
    """
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
    """,
    """
    CREATE TABLE IF NOT EXISTS m1_course_packages (
        course_package_id TEXT NOT NULL CHECK (length(course_package_id) > 0),
        package_version TEXT NOT NULL CHECK (length(package_version) > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        PRIMARY KEY (course_package_id, package_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m2_evidence_indexes (
        index_id TEXT NOT NULL CHECK (length(index_id) > 0),
        index_version TEXT NOT NULL CHECK (length(index_version) > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        PRIMARY KEY (index_id, index_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m3_knowledge_bundles (
        knowledge_bundle_id TEXT NOT NULL CHECK (length(knowledge_bundle_id) > 0),
        bundle_version TEXT NOT NULL CHECK (length(bundle_version) > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        PRIMARY KEY (knowledge_bundle_id, bundle_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m4_task_plans (
        task_id TEXT PRIMARY KEY CHECK (length(task_id) > 0),
        idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m5_learner_states (
        snapshot_id TEXT PRIMARY KEY CHECK (length(snapshot_id) > 0),
        learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
        state_version INTEGER NOT NULL CHECK (state_version > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        UNIQUE (learner_id, state_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m5_class_states (
        snapshot_id TEXT PRIMARY KEY CHECK (length(snapshot_id) > 0),
        class_id TEXT NOT NULL CHECK (length(class_id) > 0),
        aggregation_policy_version TEXT NOT NULL
            CHECK (length(aggregation_policy_version) > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m6_session_states (
        session_id TEXT NOT NULL CHECK (length(session_id) > 0),
        turn_count INTEGER NOT NULL CHECK (turn_count >= 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        PRIMARY KEY (session_id, turn_count)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m8_score_audits (
        audit_id TEXT NOT NULL CHECK (length(audit_id) > 0),
        audit_version INTEGER NOT NULL CHECK (audit_version > 0),
        item_instance_id TEXT NOT NULL CHECK (length(item_instance_id) > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        PRIMARY KEY (audit_id, audit_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS m9_teacher_reviews (
        decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
        audit_id TEXT NOT NULL CHECK (length(audit_id) > 0),
        expected_audit_version INTEGER NOT NULL CHECK (expected_audit_version > 0),
        payload TEXT NOT NULL CHECK (
            CASE WHEN json_valid(payload)
                THEN json(payload) = payload
                ELSE 0
            END
        ),
        UNIQUE (audit_id, expected_audit_version)
    )
    """,
)
_EVENT_OUTBOX_SQL = """
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
_M6_DECISION_SQL = """
CREATE TABLE IF NOT EXISTS m6_tutoring_decisions (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    turn_count INTEGER NOT NULL CHECK (turn_count >= 0),
    previous_turn_count INTEGER CHECK (
        previous_turn_count IS NULL OR previous_turn_count >= 0
    ),
    request_fingerprint TEXT NOT NULL UNIQUE
        CHECK (length(request_fingerprint) > 0),
    input_fingerprint TEXT NOT NULL UNIQUE
        CHECK (length(input_fingerprint) > 0),
    evidence_fingerprint TEXT NOT NULL
        CHECK (length(evidence_fingerprint) > 0),
    evidence_identity TEXT NOT NULL CHECK (
        CASE WHEN json_valid(evidence_identity)
            THEN json(evidence_identity) = evidence_identity
            ELSE 0
        END
    ),
    result_payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(result_payload)
            THEN json(result_payload) = result_payload
            ELSE 0
        END
    ),
    UNIQUE (session_id, turn_count),
    FOREIGN KEY (session_id, turn_count)
        REFERENCES m6_session_states(session_id, turn_count)
)
"""
_M6_DECISION_COLUMNS = (
    ("decision_id", "TEXT", 0, None, 1),
    ("session_id", "TEXT", 1, None, 0),
    ("turn_count", "INTEGER", 1, None, 0),
    ("previous_turn_count", "INTEGER", 0, None, 0),
    ("request_fingerprint", "TEXT", 1, None, 0),
    ("input_fingerprint", "TEXT", 1, None, 0),
    ("evidence_fingerprint", "TEXT", 1, None, 0),
    ("evidence_identity", "TEXT", 1, None, 0),
    ("result_payload", "TEXT", 1, None, 0),
)
_M6_DECISION_UNIQUE_KEYS = frozenset(
    {
        ("decision_id",),
        ("request_fingerprint",),
        ("input_fingerprint",),
        ("session_id", "turn_count"),
    }
)
_M6_DECISION_FOREIGN_KEY = (
    (
        0,
        "m6_session_states",
        "session_id",
        "session_id",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    ),
    (
        1,
        "m6_session_states",
        "turn_count",
        "turn_count",
        "NO ACTION",
        "NO ACTION",
        "NONE",
    ),
)


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return zero before initialization or the greatest applied version."""

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("schema_migrations",),
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


def _m6_decision_columns(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            row[4],
            int(row[5]),
        )
        for row in connection.execute(
            "PRAGMA table_info('m6_tutoring_decisions')"
        ).fetchall()
    )


def _m6_decision_unique_keys(
    connection: sqlite3.Connection,
) -> frozenset[tuple[str, ...]]:
    return frozenset(
        tuple(
            str(indexed[0])
            for indexed in connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (str(index[1]),),
            ).fetchall()
        )
        for index in connection.execute(
            "PRAGMA index_list('m6_tutoring_decisions')"
        ).fetchall()
        if int(index[2]) == 1
    )


def _m6_decision_foreign_key(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        "PRAGMA foreign_key_list('m6_tutoring_decisions')"
    ).fetchall()
    return tuple(
        (
            int(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
        )
        for row in sorted(rows, key=lambda item: int(item[1]))
    )


def _strip_sql_comments(schema_sql: str) -> str:
    """Remove SQL comments while preserving quoted literals and identifiers."""

    result: list[str] = []
    index = 0
    closing_quote: str | None = None
    while index < len(schema_sql):
        character = schema_sql[index]
        next_character = (
            schema_sql[index + 1] if index + 1 < len(schema_sql) else ""
        )
        if closing_quote is not None:
            result.append(character)
            if character == closing_quote:
                if closing_quote != "]" and next_character == closing_quote:
                    result.append(next_character)
                    index += 2
                    continue
                closing_quote = None
            index += 1
            continue
        if character in {"'", '"', "`", "["}:
            closing_quote = "]" if character == "[" else character
            result.append(character)
            index += 1
            continue
        if character == "-" and next_character == "-":
            index += 2
            while index < len(schema_sql) and schema_sql[index] not in "\r\n":
                index += 1
            continue
        if character == "/" and next_character == "*":
            comment_end = schema_sql.find("*/", index + 2)
            index = len(schema_sql) if comment_end < 0 else comment_end + 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _normalize_create_table_sql(schema_sql: str) -> str:
    normalized = "".join(
        character.casefold()
        for character in _strip_sql_comments(schema_sql)
        if not character.isspace()
    ).removesuffix(";")
    optional_prefix = "createtableifnotexists"
    if normalized.startswith(optional_prefix):
        return f"createtable{normalized[len(optional_prefix):]}"
    return normalized


def _normalized_m6_decision_schema_sql(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("m6_tutoring_decisions",),
    ).fetchone()
    schema_sql = "" if row is None or row[0] is None else str(row[0])
    return _normalize_create_table_sql(schema_sql)


def _validate_m6_decision_schema(connection: sqlite3.Connection) -> None:
    normalized_sql = _normalized_m6_decision_schema_sql(connection)
    if (
        _m6_decision_columns(connection) != _M6_DECISION_COLUMNS
        or _m6_decision_unique_keys(connection) != _M6_DECISION_UNIQUE_KEYS
        or _m6_decision_foreign_key(connection) != _M6_DECISION_FOREIGN_KEY
        or normalized_sql != _normalize_create_table_sql(_M6_DECISION_SQL)
    ):
        raise RuntimeError("M6 decision schema is incompatible")
    if connection.execute(
        "PRAGMA foreign_key_check('m6_tutoring_decisions')"
    ).fetchone() is not None:
        raise RuntimeError("M6 decision data violates foreign keys")


def migrate(connection: sqlite3.Connection) -> None:
    """Apply every pending migration in one explicit immediate transaction."""

    if connection.in_transaction:
        raise RuntimeError("migrations require an idle SQLite connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_SCHEMA_MIGRATIONS_SQL)
        version = current_schema_version(connection)
        if version > SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this application")
        if version < 1:
            for statement in _INITIAL_TABLE_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (1, _INITIAL_MIGRATION_NAME),
            )
            version = 1
        if version < 2:
            connection.execute(_EVENT_OUTBOX_SQL)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (2, _OUTBOX_MIGRATION_NAME),
            )
            version = 2
        if version < 3:
            connection.execute(_M6_DECISION_SQL)
            _validate_m6_decision_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (3, _M6_DECISION_MIGRATION_NAME),
            )
            version = 3
        else:
            _validate_m6_decision_schema(connection)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
