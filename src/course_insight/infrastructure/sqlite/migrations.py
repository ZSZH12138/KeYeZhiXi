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
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (3, _M6_DECISION_MIGRATION_NAME),
            )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
