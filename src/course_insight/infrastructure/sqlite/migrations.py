"""Deterministic explicit-transaction SQLite migrations."""

from __future__ import annotations

import hmac
import json
import sqlite3

from course_insight.infrastructure.sqlite.m5_m8_model_runtime_schema import (
    LEARNING_OBSERVATION_AUDIT_BACKFILL_SQL as _LEARNING_OBSERVATION_AUDIT_BACKFILL_SQL,
    LEARNING_OBSERVATION_AUDIT_TABLE as _LEARNING_OBSERVATION_AUDIT_TABLE,
    MODEL_RUNTIME_TABLES as _M5_M8_MODEL_RUNTIME_TABLES,
)
from course_insight.infrastructure.sqlite.m7_audit_schema import (
    M7_MODEL_INVOCATION_AUDITS_CREATED_AT_INDEX_SQL,
    M7_MODEL_INVOCATION_AUDITS_SQL,
)
from course_insight.infrastructure.sqlite.module_recovery_schema import (
    M5_CLASS_STATES_SQL as _M5_CLASS_STATES_SQL,
    M5_CLASS_STATES_V4_SQL as _M5_CLASS_STATES_V4_SQL,
    M5_LEARNER_STATES_SQL as _M5_LEARNER_STATES_SQL,
    M5_LEARNER_STATES_V4_SQL as _M5_LEARNER_STATES_V4_SQL,
    M9_MODEL_INVOCATION_AUDITS_SQL as _M9_MODEL_INVOCATION_AUDITS_SQL,
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
    ASSESSMENT_RUNS_NONTERMINAL_RESCORE_INDEX_SQL as _M0_ASSESSMENT_RUNS_NONTERMINAL_RESCORE_INDEX_SQL,
    ASSESSMENT_RUNS_V5_SQL as _M0_ASSESSMENT_RUNS_V5_SQL,
    ASSESSMENT_RUNS_V6_SQL as _M0_ASSESSMENT_RUNS_V6_SQL,
    ASSESSMENT_RUNS_V11_SQL as _M0_ASSESSMENT_RUNS_SQL,
    ASSESSMENT_RUNS_V18_SQL as _M0_ASSESSMENT_RUNS_V18_SQL,
    ASSESSMENT_RUNS_V19_SQL as _M0_ASSESSMENT_RUNS_V19_SQL,
    ASSESSMENT_RUNS_V9_SQL as _M0_ASSESSMENT_RUNS_V9_SQL,
    ASSESSMENT_RUNS_SUBMIT_INDEX_SQL as _M0_ASSESSMENT_RUNS_SUBMIT_INDEX_SQL,
    migrate_workflow_v5_to_v6,
    migrate_workflow_v7_to_v8,
    migrate_workflow_v8_to_v9,
    migrate_workflow_v10_to_v11,
    migrate_workflow_v17_to_v18,
    migrate_workflow_v18_to_v19,
)
from course_insight.infrastructure.sqlite.migrations_s1_s6 import (
    ensure_schema as _ensure_s1_s6_schema,
)

SCHEMA_VERSION = 19


def _applied_assessment_runs_schema_version(applied_versions: set[int]) -> int:
    """Select the CREATE SQL that matches the latest applied workflow rebuild."""

    if 19 in applied_versions:
        return 19
    if 18 in applied_versions:
        return 18
    if 13 in applied_versions:
        return 13
    if 9 in applied_versions:
        return 9
    if 8 in applied_versions:
        return 8
    return min(max(applied_versions), 7)


_INITIAL_MIGRATION_NAME = "initial_module_tables"
_OUTBOX_MIGRATION_NAME = "m0_event_outbox"
_M6_DECISION_MIGRATION_NAME = "m6_tutoring_decisions"
_M4_INTENT_DECISION_MIGRATION_NAME = "m4_intent_decisions"
_M4_INTENT_STATUS_MIGRATION_NAME = "m4_intent_runtime_statuses"
_MODULE_RECOVERY_MIGRATION_NAME = "module_owned_recovery"
_ASSESSMENT_WORKFLOW_MIGRATION_NAME = "m0_assessment_workflow"
_ASSESSMENT_WORKFLOW_REFS_MIGRATION_NAME = "m0_assessment_workflow_refs"
_ASSESSMENT_WORKFLOW_REVIEW_GUARD_MIGRATION_NAME = (
    "m0_assessment_workflow_review_guard"
)
_ASSESSMENT_WORKFLOW_RECOVERY_FREEZE_MIGRATION_NAME = (
    "m0_assessment_workflow_recovery_freeze"
)
_M6_POLICY_LEARNING_MIGRATION_NAME = "m6_policy_learning"
_ASSESSMENT_POLICY_FREEZE_MIGRATION_NAME = "m0_policy_freeze"
_M5_M8_MODEL_RUNTIME_MIGRATION_NAME = "m5_m8_model_runtime"
_M5_LEARNING_OBSERVATION_AUDIT_MIGRATION_NAME = (
    "m5_learning_observation_audit_identity"
)
_M9_MODEL_AUDIT_MIGRATION_NAME = "m9_model_invocation_audits"
_M7_MODEL_AUDIT_MIGRATION_NAME = "m7_model_invocation_audits"
_M0_WAITING_ROOMS_MIGRATION_NAME = "m0_waiting_rooms"
_M0_RESCORE_OPERATION_MIGRATION_NAME = "m0_rescore_operation"
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
_M4_INTENT_DECISION_V10_SQL = """
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
_M4_INTENT_DECISION_SQL = _M4_INTENT_DECISION_V10_SQL.replace(
    "decision_status IN ('accepted', 'abstained', 'out_of_scope', 'invalid')",
    (
        "decision_status IN ("
        "'accepted', 'abstained', 'out_of_scope', "
        "'unavailable', 'failed', 'invalid'"
        ")"
    ),
)
_M4_INTENT_DECISION_V11_STAGING_SQL = _M4_INTENT_DECISION_SQL.replace(
    "CREATE TABLE IF NOT EXISTS m4_intent_decisions",
    "CREATE TABLE m4_intent_decisions_v11",
    1,
)
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
_M6_POLICY_TABLES = (
    (
        "m6_policy_artifacts",
        """
        CREATE TABLE IF NOT EXISTS m6_policy_artifacts (
            policy_id TEXT PRIMARY KEY CHECK (length(policy_id) > 0),
            artifact_sha256 TEXT NOT NULL UNIQUE CHECK (
                length(artifact_sha256) = 64
                AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            payload TEXT NOT NULL CHECK (
                CASE WHEN json_valid(payload)
                    THEN json(payload) = payload
                    ELSE 0
                END
            ),
            payload_checksum TEXT NOT NULL CHECK (
                length(payload_checksum) = 64
                AND payload_checksum NOT GLOB '*[^0-9a-f]*'
            )
        )
        """,
    ),
    (
        "m6_policy_executions",
        """
        CREATE TABLE IF NOT EXISTS m6_policy_executions (
            request_fingerprint TEXT PRIMARY KEY CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            policy_execution_fingerprint TEXT NOT NULL UNIQUE CHECK (
                length(policy_execution_fingerprint) = 64
                AND policy_execution_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            payload TEXT NOT NULL CHECK (
                CASE WHEN json_valid(payload)
                    THEN json(payload) = payload
                    ELSE 0
                END
            ),
            payload_checksum TEXT NOT NULL CHECK (
                length(payload_checksum) = 64
                AND payload_checksum NOT GLOB '*[^0-9a-f]*'
            )
        )
        """,
    ),
    (
        "m6_policy_observations",
        """
        CREATE TABLE IF NOT EXISTS m6_policy_observations (
            decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
            request_fingerprint TEXT NOT NULL UNIQUE CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            policy_execution_fingerprint TEXT NOT NULL UNIQUE CHECK (
                length(policy_execution_fingerprint) = 64
                AND policy_execution_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            payload TEXT NOT NULL CHECK (
                CASE WHEN json_valid(payload)
                    THEN json(payload) = payload
                    ELSE 0
                END
            ),
            payload_checksum TEXT NOT NULL CHECK (
                length(payload_checksum) = 64
                AND payload_checksum NOT GLOB '*[^0-9a-f]*'
            ),
            FOREIGN KEY (decision_id)
                REFERENCES m6_tutoring_decisions(decision_id),
            FOREIGN KEY (request_fingerprint)
                REFERENCES m6_policy_executions(request_fingerprint),
            FOREIGN KEY (policy_execution_fingerprint)
                REFERENCES m6_policy_executions(
                    policy_execution_fingerprint
                )
        )
        """,
    ),
    (
        "m6_policy_rewards",
        """
        CREATE TABLE IF NOT EXISTS m6_policy_rewards (
            reward_identity TEXT PRIMARY KEY CHECK (
                length(reward_identity) = 64
                AND reward_identity NOT GLOB '*[^0-9a-f]*'
            ),
            policy_execution_fingerprint TEXT NOT NULL CHECK (
                length(policy_execution_fingerprint) = 64
                AND policy_execution_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            reward_version TEXT NOT NULL CHECK (length(reward_version) > 0),
            payload TEXT NOT NULL CHECK (
                CASE WHEN json_valid(payload)
                    THEN json(payload) = payload
                    ELSE 0
                END
            ),
            payload_checksum TEXT NOT NULL CHECK (
                length(payload_checksum) = 64
                AND payload_checksum NOT GLOB '*[^0-9a-f]*'
            ),
            UNIQUE (policy_execution_fingerprint, reward_version),
            FOREIGN KEY (policy_execution_fingerprint)
                REFERENCES m6_policy_executions(
                    policy_execution_fingerprint
                )
        )
        """,
    ),
    (
        "m6_policy_evaluations",
        """
        CREATE TABLE IF NOT EXISTS m6_policy_evaluations (
            evaluation_identity TEXT PRIMARY KEY CHECK (
                length(evaluation_identity) = 64
                AND evaluation_identity NOT GLOB '*[^0-9a-f]*'
            ),
            policy_id TEXT NOT NULL CHECK (length(policy_id) > 0),
            dataset_identity TEXT NOT NULL CHECK (
                length(dataset_identity) > 0
            ),
            payload TEXT NOT NULL CHECK (
                CASE WHEN json_valid(payload)
                    THEN json(payload) = payload
                    ELSE 0
                END
            ),
            payload_checksum TEXT NOT NULL CHECK (
                length(payload_checksum) = 64
                AND payload_checksum NOT GLOB '*[^0-9a-f]*'
            ),
            UNIQUE (policy_id, dataset_identity)
        )
        """,
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


def _normalize_create_index_sql(schema_sql: str) -> str:
    normalized = "".join(
        character.casefold()
        for character in _strip_sql_comments(schema_sql)
        if not character.isspace()
    ).removesuffix(";")
    optional_prefix = "createindexifnotexists"
    if normalized.startswith(optional_prefix):
        return f"createindex{normalized[len(optional_prefix):]}"
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


def _normalized_index_schema_sql(
    connection: sqlite3.Connection,
    index_name: str,
) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    schema_sql = "" if row is None or row[0] is None else str(row[0])
    return _normalize_create_index_sql(schema_sql)


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
        _M0_ASSESSMENT_RUNS_V19_SQL
        if schema_version >= 19
        else (
            _M0_ASSESSMENT_RUNS_V18_SQL
            if schema_version >= 18
            else (
                _M0_ASSESSMENT_RUNS_SQL
                if schema_version >= 13
                else (
                    _M0_ASSESSMENT_RUNS_V9_SQL
                    if schema_version >= 9
                    else _M0_ASSESSMENT_RUNS_V6_SQL
                )
            )
        )
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
    if schema_version < 19:
        return
    rescore_index_row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index'
          AND name = 'm0_one_nonterminal_rescore_per_paper'
        """
    ).fetchone()
    if rescore_index_row is None or _normalize_create_table_sql(
        str(rescore_index_row[0])
    ) != _normalize_create_table_sql(
        _M0_ASSESSMENT_RUNS_NONTERMINAL_RESCORE_INDEX_SQL
    ):
        raise RuntimeError(
            "m0 assessment rescore linearity is incompatible"
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


def _validate_m6_policy_schema(connection: sqlite3.Connection) -> None:
    for table_name, expected_sql in _M6_POLICY_TABLES:
        if _normalized_table_schema_sql(
            connection,
            table_name,
        ) != _normalize_create_table_sql(expected_sql):
            raise RuntimeError(f"{table_name} schema is incompatible")
    for table_name, _ in _M6_POLICY_TABLES:
        if connection.execute(
            "SELECT 1 FROM pragma_foreign_key_check(?)",
            (table_name,),
        ).fetchone() is not None:
            raise RuntimeError(f"{table_name} data violates foreign keys")


def _validate_m4_intent_decision_schema(
    connection: sqlite3.Connection,
    *,
    expected_sql: str = _M4_INTENT_DECISION_SQL,
) -> None:
    actual_sql = _normalized_table_schema_sql(
        connection,
        "m4_intent_decisions",
    ).replace('"m4_intent_decisions"', "m4_intent_decisions")
    if actual_sql != _normalize_create_table_sql(expected_sql):
        raise RuntimeError("M4 intent decision schema is incompatible")


def _migrate_m4_intent_runtime_statuses(
    connection: sqlite3.Connection,
) -> None:
    """Extend the M4 status constraint without losing v10 decisions."""

    connection.execute(_M4_INTENT_DECISION_V11_STAGING_SQL)
    connection.execute(
        """
        INSERT INTO m4_intent_decisions_v11(
            request_key,
            resolved_task_type,
            decision_status,
            decision_source,
            adapter_id,
            adapter_version,
            policy_version,
            confidence,
            margin,
            input_checksum,
            reason_codes_json,
            shadow_json,
            schema_version,
            payload_checksum,
            created_at
        )
        SELECT
            request_key,
            resolved_task_type,
            decision_status,
            decision_source,
            adapter_id,
            adapter_version,
            policy_version,
            confidence,
            margin,
            input_checksum,
            reason_codes_json,
            shadow_json,
            schema_version,
            payload_checksum,
            created_at
        FROM m4_intent_decisions
        """
    )
    connection.execute("DROP TABLE m4_intent_decisions")
    connection.execute(
        "ALTER TABLE m4_intent_decisions_v11 RENAME TO m4_intent_decisions"
    )


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


def _validate_m5_m8_model_runtime_schema(
    connection: sqlite3.Connection,
) -> None:
    for table_name, expected_sql in _M5_M8_MODEL_RUNTIME_TABLES:
        if _normalized_table_schema_sql(
            connection,
            table_name,
        ) != _normalize_create_table_sql(expected_sql):
            raise RuntimeError(f"{table_name} schema is incompatible")


def _validate_learning_observation_audit_schema(
    connection: sqlite3.Connection,
) -> None:
    table_name, expected_sql = _LEARNING_OBSERVATION_AUDIT_TABLE
    if _normalized_table_schema_sql(
        connection,
        table_name,
    ) != _normalize_create_table_sql(expected_sql):
        raise RuntimeError(f"{table_name} schema is incompatible")


def _validate_m9_model_audit_schema(
    connection: sqlite3.Connection,
) -> None:
    actual_sql = _normalized_table_schema_sql(
        connection,
        "m9_model_invocation_audits",
    ).replace('"m9_model_invocation_audits"', "m9_model_invocation_audits")
    expected_sql = _normalize_create_table_sql(_M9_MODEL_INVOCATION_AUDITS_SQL)
    if actual_sql != expected_sql:
        raise RuntimeError(
            "m9_model_invocation_audits schema is incompatible"
        )
    if connection.execute(
        "PRAGMA foreign_key_check('m9_model_invocation_audits')"
    ).fetchone() is not None:
        raise RuntimeError("M9 model audit data violates foreign keys")
    from course_insight.contracts.analytics import TeacherAnalyticsBundle

    for row in connection.execute(
        """
        SELECT audits.source_report_checksum AS source_report_checksum,
               reports.payload AS payload
        FROM m9_model_invocation_audits AS audits
        JOIN m9_teacher_analytics AS reports
          ON reports.report_id = audits.source_report_id
        """
    ):
        try:
            payload = json.loads(str(row["payload"]))
            bundle = TeacherAnalyticsBundle.model_validate(payload)
            expected = bundle.content_checksum()
        except Exception as error:
            raise RuntimeError("source report checksum mismatch") from error
        if not hmac.compare_digest(str(row["source_report_checksum"]), expected):
            raise RuntimeError("source report checksum mismatch")


def _validate_m7_model_audit_schema(
    connection: sqlite3.Connection,
) -> None:
    actual_table_sql = _normalized_table_schema_sql(
        connection,
        "m7_model_invocation_audits",
    )
    expected_table_sql = _normalize_create_table_sql(
        M7_MODEL_INVOCATION_AUDITS_SQL
    )
    if actual_table_sql != expected_table_sql:
        raise RuntimeError(
            "m7_model_invocation_audits schema is incompatible"
        )
    actual_index_sql = _normalized_index_schema_sql(
        connection,
        "m7_model_invocation_audits_created_at_idx",
    )
    expected_index_sql = _normalize_create_index_sql(
        M7_MODEL_INVOCATION_AUDITS_CREATED_AT_INDEX_SQL
    )
    if actual_index_sql != expected_index_sql:
        raise RuntimeError(
            "m7_model_invocation_audits index is incompatible"
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
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions
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
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions | {8}
                ),
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (8, _ASSESSMENT_WORKFLOW_REVIEW_GUARD_MIGRATION_NAME),
            )
            applied_versions.add(8)
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions
                ),
            )
        if 9 not in applied_versions:
            migrate_workflow_v8_to_v9(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions | {9}
                ),
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (9, _ASSESSMENT_WORKFLOW_RECOVERY_FREEZE_MIGRATION_NAME),
            )
            applied_versions.add(9)
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions
                ),
            )
        if 10 not in applied_versions:
            connection.execute(_M4_INTENT_DECISION_V10_SQL)
            try:
                _validate_m4_intent_decision_schema(
                    connection,
                    expected_sql=_M4_INTENT_DECISION_V10_SQL,
                )
            except RuntimeError:
                # Recovery tests and repaired ledgers can retain the v11 table
                # while replaying the forward-only migration sequence.
                _validate_m4_intent_decision_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (10, _M4_INTENT_DECISION_MIGRATION_NAME),
            )
            applied_versions.add(10)
        else:
            _validate_m4_intent_decision_schema(
                connection,
                expected_sql=(
                    _M4_INTENT_DECISION_SQL
                    if 11 in applied_versions
                    else _M4_INTENT_DECISION_V10_SQL
                ),
            )
        if 11 not in applied_versions:
            _migrate_m4_intent_runtime_statuses(connection)
            _validate_m4_intent_decision_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (11, _M4_INTENT_STATUS_MIGRATION_NAME),
            )
            applied_versions.add(11)
        else:
            _validate_m4_intent_decision_schema(connection)
        if 12 not in applied_versions:
            for _, statement in _M6_POLICY_TABLES:
                connection.execute(statement)
            _validate_m6_policy_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (12, _M6_POLICY_LEARNING_MIGRATION_NAME),
            )
            applied_versions.add(12)
        else:
            _validate_m6_policy_schema(connection)
        if 13 not in applied_versions:
            migrate_workflow_v10_to_v11(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions | {13}
                ),
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (13, _ASSESSMENT_POLICY_FREEZE_MIGRATION_NAME),
            )
            applied_versions.add(13)
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=_applied_assessment_runs_schema_version(
                    applied_versions
                ),
            )
        if 14 not in applied_versions:
            for _, statement in _M5_M8_MODEL_RUNTIME_TABLES:
                connection.execute(statement)
            _validate_m5_m8_model_runtime_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (14, _M5_M8_MODEL_RUNTIME_MIGRATION_NAME),
            )
            applied_versions.add(14)
        else:
            _validate_m5_m8_model_runtime_schema(connection)
        if 15 not in applied_versions:
            connection.execute(_LEARNING_OBSERVATION_AUDIT_TABLE[1])
            connection.execute(_LEARNING_OBSERVATION_AUDIT_BACKFILL_SQL)
            _validate_learning_observation_audit_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (15, _M5_LEARNING_OBSERVATION_AUDIT_MIGRATION_NAME),
            )
            applied_versions.add(15)
        else:
            _validate_learning_observation_audit_schema(connection)
        if 16 not in applied_versions:
            connection.execute(_M9_MODEL_INVOCATION_AUDITS_SQL)
            _validate_m9_model_audit_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (16, _M9_MODEL_AUDIT_MIGRATION_NAME),
            )
            applied_versions.add(16)
        else:
            _validate_m9_model_audit_schema(connection)
        if 17 not in applied_versions:
            connection.execute(M7_MODEL_INVOCATION_AUDITS_SQL)
            connection.execute(M7_MODEL_INVOCATION_AUDITS_CREATED_AT_INDEX_SQL)
            _validate_m7_model_audit_schema(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (17, _M7_MODEL_AUDIT_MIGRATION_NAME),
            )
            applied_versions.add(17)
        else:
            _validate_m7_model_audit_schema(connection)
        if 18 not in applied_versions:
            migrate_workflow_v17_to_v18(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=18,
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (18, _M0_WAITING_ROOMS_MIGRATION_NAME),
            )
            applied_versions.add(18)
        if 19 not in applied_versions:
            migrate_workflow_v18_to_v19(connection)
            _validate_assessment_workflow_schema(
                connection,
                schema_version=19,
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (19, _M0_RESCORE_OPERATION_MIGRATION_NAME),
            )
            applied_versions.add(19)
        else:
            _validate_assessment_workflow_schema(
                connection,
                schema_version=19,
            )
        # M1-M3 S1-S6 owns an independent version ledger. Apply its artifact
        # tables in the same transaction without consuming platform versions.
        _ensure_s1_s6_schema(connection)
        validate_outbox_schema(connection, schema_version=SCHEMA_VERSION)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
