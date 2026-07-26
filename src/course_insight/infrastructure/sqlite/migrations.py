"""Deterministic explicit-transaction SQLite migrations."""

from __future__ import annotations

import sqlite3

from course_insight.infrastructure.sqlite.module_recovery_schema import (
    M5_CLASS_STATES_SQL as _M5_CLASS_STATES_SQL,
    M5_CLASS_STATES_V4_SQL as _M5_CLASS_STATES_V4_SQL,
    M5_LEARNER_STATES_SQL as _M5_LEARNER_STATES_SQL,
    M5_LEARNER_STATES_V4_SQL as _M5_LEARNER_STATES_V4_SQL,
    MODULE_RECOVERY_TABLES as _MODULE_RECOVERY_TABLES,
)
from course_insight.infrastructure.sqlite.outbox_migration import (
    LEARNING_EVENTS_SQL as _M0_LEARNING_EVENTS_SQL,
    LEGACY_OUTBOX_SQL as _EVENT_OUTBOX_SQL,
    OUTBOX_V7_MIGRATION_NAME as _OUTBOX_V7_MIGRATION_NAME,
    migrate_outbox_v6_to_v7,
    validate_m0_storage_schema,
    validate_outbox_schema,
)
from course_insight.infrastructure.sqlite.workflow_migration import (
    ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL as _M0_ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL,
    ASSESSMENT_RUNS_V5_SQL as _M0_ASSESSMENT_RUNS_V5_SQL,
    ASSESSMENT_RUNS_V6_SQL as _M0_ASSESSMENT_RUNS_V6_SQL,
    ASSESSMENT_RUNS_V9_SQL as _M0_ASSESSMENT_RUNS_SQL,
    ASSESSMENT_RUNS_SUBMIT_INDEX_SQL as _M0_ASSESSMENT_RUNS_SUBMIT_INDEX_SQL,
    migrate_workflow_v5_to_v6,
    migrate_workflow_v7_to_v8,
    migrate_workflow_v8_to_v9,
)

SCHEMA_VERSION = 10
_INITIAL_MIGRATION_NAME = "initial_module_tables"
_OUTBOX_MIGRATION_NAME = "m0_event_outbox"
_M6_DECISION_MIGRATION_NAME = "m6_tutoring_decisions"
_M4_INTENT_DECISION_MIGRATION_NAME = "m4_intent_decisions"
_MODULE_RECOVERY_MIGRATION_NAME = "module_owned_recovery"
_ASSESSMENT_WORKFLOW_MIGRATION_NAME = "m0_assessment_workflow"
_ASSESSMENT_WORKFLOW_REFS_MIGRATION_NAME = "m0_assessment_workflow_refs"
_ASSESSMENT_WORKFLOW_REVIEW_GUARD_MIGRATION_NAME = (
    "m0_assessment_workflow_review_guard"
)
_ASSESSMENT_WORKFLOW_RECOVERY_FREEZE_MIGRATION_NAME = (
    "m0_assessment_workflow_recovery_freeze"
)
_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE CHECK (length(name) > 0)
)
"""
_INITIAL_TABLE_STATEMENTS = (
    _M0_LEARNING_EVENTS_SQL,
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
_M4_INTENT_DECISION_SQL = """
CREATE TABLE IF NOT EXISTS m4_intent_decisions (
    request_key TEXT PRIMARY KEY CHECK (length(trim(request_key)) > 0),
    resolved_task_type TEXT NULL,
    decision_status TEXT NOT NULL,
    decision_source TEXT NOT NULL CHECK (length(trim(decision_source)) > 0),
    adapter_id TEXT NOT NULL CHECK (length(trim(adapter_id)) > 0),
    adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
    confidence REAL NULL CHECK (
        confidence IS NULL
        OR (
            typeof(confidence) IN ('real', 'integer')
            AND confidence >= 0.0
            AND confidence <= 1.0
        )
    ),
    margin REAL NULL CHECK (
        margin IS NULL
        OR (
            typeof(margin) IN ('real', 'integer')
            AND margin >= 0.0
            AND margin <= 1.0
        )
    ),
    input_checksum TEXT NOT NULL CHECK (
        length(input_checksum) = 64
        AND input_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    reason_codes_json TEXT NOT NULL CHECK (
        CASE WHEN json_valid(reason_codes_json)
            THEN json_type(reason_codes_json) = 'array'
                AND json(reason_codes_json) = reason_codes_json
            ELSE 0
        END
    ),
    shadow_json TEXT NULL CHECK (
        shadow_json IS NULL
        OR CASE WHEN json_valid(shadow_json)
            THEN json_type(shadow_json) = 'object'
                AND json(shadow_json) = shadow_json
            ELSE 0
        END
    ),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    payload_checksum TEXT NOT NULL CHECK (
        length(payload_checksum) = 64
        AND payload_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK (
        datetime(created_at) IS NOT NULL
        AND substr(created_at, -6) = '+00:00'
    ),
    CHECK (
        decision_status IN ('accepted', 'abstained', 'out_of_scope', 'invalid')
    ),
    CHECK (
        resolved_task_type IS NULL
        OR resolved_task_type IN (
            'qa',
            'diagnostic',
            'practice',
            'correction',
            'stage_assessment'
        )
    ),
    CHECK (
        (decision_status = 'accepted' AND resolved_task_type IS NOT NULL)
        OR (decision_status != 'accepted' AND resolved_task_type IS NULL)
    ),
    CHECK (
        (
            decision_status = 'accepted'
            AND decision_source IN (
                'legal_hint',
                'high_precision_rule',
                'legacy_rule',
                'active_model'
            )
        )
        OR (decision_status != 'accepted' AND decision_source = 'refusal')
    ),
    CHECK (
        decision_source != 'active_model'
        OR (confidence IS NOT NULL AND margin IS NOT NULL)
    )
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


def _applied_schema_versions(
    connection: sqlite3.Connection,
) -> set[int]:
    return {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }


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


def _normalized_table_schema_sql(
    connection: sqlite3.Connection,
    table_name: str,
) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    schema_sql = "" if row is None or row[0] is None else str(row[0])
    return _normalize_create_table_sql(schema_sql)


def validate_m0_schema(connection: sqlite3.Connection) -> None:
    """Reject missing or weakened M0 persistence structures."""

    validate_m0_storage_schema(
        connection,
        schema_version=current_schema_version(connection),
    )
    if current_schema_version(connection) >= 6:
        _validate_assessment_workflow_schema(
            connection,
            schema_version=current_schema_version(connection),
        )


def _validate_assessment_workflow_schema(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    expected_sql = (
        _M0_ASSESSMENT_RUNS_SQL
        if schema_version >= 9
        else _M0_ASSESSMENT_RUNS_V6_SQL
    )
    if _normalized_table_schema_sql(
        connection,
        "m0_assessment_runs",
    ) != _normalize_create_table_sql(expected_sql):
        raise RuntimeError(
            "m0_assessment_runs schema is incompatible with workflow recovery"
        )
    index_row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index' AND name = 'm0_one_submit_per_paper'
        """
    ).fetchone()
    if index_row is None or _normalize_create_table_sql(
        str(index_row[0])
    ) != _normalize_create_table_sql(_M0_ASSESSMENT_RUNS_SUBMIT_INDEX_SQL):
        raise RuntimeError(
            "m0 assessment submit uniqueness is incompatible"
        )
    if schema_version < 8:
        return
    review_index_row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index'
          AND name = 'm0_one_nonterminal_review_per_paper'
        """
    ).fetchone()
    if review_index_row is None or _normalize_create_table_sql(
        str(review_index_row[0])
    ) != _normalize_create_table_sql(
        _M0_ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL
    ):
        raise RuntimeError(
            "m0 assessment review linearity is incompatible"
        )


def _normalized_m6_decision_schema_sql(connection: sqlite3.Connection) -> str:
    return _normalized_table_schema_sql(connection, "m6_tutoring_decisions")


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


def _validate_m4_intent_decision_schema(
    connection: sqlite3.Connection,
) -> None:
    if _normalized_table_schema_sql(
        connection,
        "m4_intent_decisions",
    ) != _normalize_create_table_sql(_M4_INTENT_DECISION_SQL):
        raise RuntimeError("M4 intent decision schema is incompatible")


def _migrate_m5_learner_scope(connection: sqlite3.Connection) -> None:
    """Replace the legacy learner-only uniqueness key without losing rows."""

    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('m5_learner_states')"
        ).fetchall()
    }
    if {"course_id", "class_id"} <= columns:
        return
    connection.execute(_M5_LEARNER_STATES_V4_SQL)
    connection.execute(
        """
        INSERT INTO m5_learner_states_v4(
            snapshot_id,
            course_id,
            class_id,
            learner_id,
            state_version,
            payload
        )
        SELECT
            snapshot_id,
            json_extract(payload, '$.course_id'),
            json_extract(payload, '$.class_id'),
            learner_id,
            state_version,
            payload
        FROM m5_learner_states
        """
    )
    connection.execute("DROP TABLE m5_learner_states")
    connection.execute(
        "ALTER TABLE m5_learner_states_v4 RENAME TO m5_learner_states"
    )


def _migrate_m5_class_scope(connection: sqlite3.Connection) -> None:
    """Add course-aware authority to legacy class-state identities."""

    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('m5_class_states')"
        ).fetchall()
    }
    if {"course_id", "state_version"} <= columns:
        return
    connection.execute(_M5_CLASS_STATES_V4_SQL)
    connection.execute(
        """
        INSERT INTO m5_class_states_v4(
            snapshot_id,
            course_id,
            class_id,
            state_version,
            aggregation_policy_version,
            payload
        )
        SELECT
            snapshot_id,
            json_extract(payload, '$.course_id'),
            class_id,
            ROW_NUMBER() OVER (
                PARTITION BY json_extract(payload, '$.course_id'), class_id
                ORDER BY
                    julianday(json_extract(payload, '$.updated_at')),
                    snapshot_id
            ),
            aggregation_policy_version,
            payload
        FROM m5_class_states
        """
    )
    connection.execute("DROP TABLE m5_class_states")
    connection.execute(
        "ALTER TABLE m5_class_states_v4 RENAME TO m5_class_states"
    )


def _validate_module_recovery_schema(connection: sqlite3.Connection) -> None:
    learner_schema = _normalized_table_schema_sql(
        connection,
        "m5_learner_states",
    ).replace('"m5_learner_states"', "m5_learner_states")
    if learner_schema != _normalize_create_table_sql(
        _M5_LEARNER_STATES_SQL
    ):
        raise RuntimeError(
            "m5_learner_states schema is incompatible with module recovery"
        )
    class_schema = _normalized_table_schema_sql(
        connection,
        "m5_class_states",
    ).replace('"m5_class_states"', "m5_class_states")
    if class_schema != _normalize_create_table_sql(_M5_CLASS_STATES_SQL):
        raise RuntimeError(
            "m5_class_states schema is incompatible with module recovery"
        )
    for table_name, expected_sql in _MODULE_RECOVERY_TABLES:
        if _normalized_table_schema_sql(
            connection,
            table_name,
        ) != _normalize_create_table_sql(expected_sql):
            raise RuntimeError(
                f"{table_name} schema is incompatible with module recovery"
            )


def migrate(connection: sqlite3.Connection) -> None:
    """Apply every pending migration in one explicit immediate transaction."""

    if connection.in_transaction:
        raise RuntimeError("migrations require an idle SQLite connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_SCHEMA_MIGRATIONS_SQL)
        version = current_schema_version(connection)
        applied_versions = _applied_schema_versions(connection)
        if version > SCHEMA_VERSION:
            raise RuntimeError("database schema is newer than this application")
        if 1 not in applied_versions:
            for statement in _INITIAL_TABLE_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (1, _INITIAL_MIGRATION_NAME),
            )
            applied_versions.add(1)
        if 2 not in applied_versions:
            connection.execute(_EVENT_OUTBOX_SQL)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (2, _OUTBOX_MIGRATION_NAME),
            )
            applied_versions.add(2)
        validate_m0_schema(connection)
        if 3 not in applied_versions:
            connection.execute(_M6_DECISION_SQL)
            _validate_m6_decision_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (3, _M6_DECISION_MIGRATION_NAME),
            )
            applied_versions.add(3)
        else:
            _validate_m6_decision_schema(connection)
        if 4 not in applied_versions:
            _migrate_m5_learner_scope(connection)
            _migrate_m5_class_scope(connection)
            for _, statement in _MODULE_RECOVERY_TABLES:
                connection.execute(statement)
            _validate_module_recovery_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (4, _MODULE_RECOVERY_MIGRATION_NAME),
            )
            applied_versions.add(4)
        else:
            _validate_module_recovery_schema(connection)
        if 5 not in applied_versions:
            connection.execute(_M0_ASSESSMENT_RUNS_V5_SQL)
            connection.execute(_M0_ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (5, _ASSESSMENT_WORKFLOW_MIGRATION_NAME),
            )
            applied_versions.add(5)
        if 6 not in applied_versions:
            migrate_workflow_v5_to_v6(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=6,
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (6, _ASSESSMENT_WORKFLOW_REFS_MIGRATION_NAME),
            )
            applied_versions.add(6)
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=(
                    9
                    if 9 in applied_versions
                    else min(max(applied_versions), 7)
                ),
            )
        if 7 not in applied_versions:
            migrate_outbox_v6_to_v7(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (7, _OUTBOX_V7_MIGRATION_NAME),
            )
            applied_versions.add(7)
        validate_outbox_schema(connection, schema_version=7)
        if 8 not in applied_versions:
            migrate_workflow_v7_to_v8(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=9 if 9 in applied_versions else 8,
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (8, _ASSESSMENT_WORKFLOW_REVIEW_GUARD_MIGRATION_NAME),
            )
            applied_versions.add(8)
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=9 if 9 in applied_versions else 8,
            )
        if 9 not in applied_versions:
            migrate_workflow_v8_to_v9(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=9,
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (9, _ASSESSMENT_WORKFLOW_RECOVERY_FREEZE_MIGRATION_NAME),
            )
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=SCHEMA_VERSION,
            )
        if 10 not in applied_versions:
            connection.execute(_M4_INTENT_DECISION_SQL)
            _validate_m4_intent_decision_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (10, _M4_INTENT_DECISION_MIGRATION_NAME),
            )
        else:
            _validate_m4_intent_decision_schema(connection)
        validate_outbox_schema(connection, schema_version=SCHEMA_VERSION)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
