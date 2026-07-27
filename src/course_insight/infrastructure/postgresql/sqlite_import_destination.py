"""Fixed-allowlist PostgreSQL destination for SQLite migration batches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.postgresql.base import PostgresError
from course_insight.infrastructure.postgresql.pool import PostgresPool


_COLUMNS = {
    "m0_learning_events": (
        "event_id",
        "idempotency_key",
        "event_type",
        "occurred_at",
        "payload",
    ),
    "m0_event_outbox": (
        "event_id",
        "record",
        "status",
        "attempt_count",
        "version",
        "available_at",
        "locked_by",
        "locked_at",
        "lease_until",
        "last_error_code",
        "created_at",
        "updated_at",
    ),
    "m0_assessment_runs": (
        "operation_id",
        "operation",
        "request_checksum",
        "course_id",
        "class_id",
        "learner_id",
        "session_id",
        "task_id",
        "paper_id",
        "attempt_id",
        "feedback_id",
        "report_id",
        "scoring_result_checksum",
        "target_audit_id",
        "target_audit_version",
        "state_version",
        "knowledge_bundle_id",
        "knowledge_bundle_version",
        "knowledge_bundle_checksum",
        "course_package_id",
        "evidence_index_id",
        "evidence_index_version",
        "evidence_index_checksum",
        "state_policy_checksum",
        "teacher_policy_checksum",
        "previous_state_frozen",
        "previous_learner_snapshot_id",
        "previous_learner_state_version",
        "previous_class_snapshot_id",
        "previous_class_state_version",
        "policy_id",
        "adapter_id",
        "adapter_version",
        "artifact_sha256",
        "feature_schema_version",
        "action_space_version",
        "gate_policy_version",
        "checkpoint",
        "status",
        "version",
        "locked_by",
        "lease_until",
        "error_code",
        "created_at",
        "updated_at",
    ),
    "m4_task_plans": (
        "task_id",
        "idempotency_key",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m5_learner_states": (
        "snapshot_id",
        "course_id",
        "class_id",
        "learner_id",
        "state_version",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m5_class_states": (
        "snapshot_id",
        "course_id",
        "class_id",
        "state_version",
        "aggregation_policy_version",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m5_state_updates": (
        "attempt_id",
        "course_id",
        "class_id",
        "learner_id",
        "state_version",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m6_session_states": (
        "session_id",
        "turn_count",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m6_policy_artifacts": (
        "policy_id",
        "artifact_sha256",
        "payload",
        "payload_checksum",
    ),
    "m6_policy_executions": (
        "request_fingerprint",
        "policy_execution_fingerprint",
        "payload",
        "payload_checksum",
    ),
    "m6_tutoring_decisions": (
        "decision_id",
        "session_id",
        "turn_count",
        "previous_turn_count",
        "request_fingerprint",
        "input_fingerprint",
        "evidence_fingerprint",
        "evidence_identity",
        "result_payload",
        "payload_checksum",
        "schema_version",
    ),
    "m6_policy_observations": (
        "decision_id",
        "request_fingerprint",
        "policy_execution_fingerprint",
        "payload",
        "payload_checksum",
    ),
    "m6_policy_rewards": (
        "reward_identity",
        "policy_execution_fingerprint",
        "reward_version",
        "payload",
        "payload_checksum",
    ),
    "m6_policy_evaluations": (
        "evaluation_identity",
        "policy_id",
        "dataset_identity",
        "payload",
        "payload_checksum",
    ),
    "m7_student_feedback": (
        "feedback_id",
        "task_id",
        "learner_id",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m8_assessment_papers": (
        "paper_id",
        "task_id",
        "course_id",
        "class_id",
        "learner_id",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m8_score_audits": (
        "audit_id",
        "audit_version",
        "item_instance_id",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m8_scoring_results": (
        "attempt_id",
        "result_key",
        "paper_id",
        "learner_id",
        "finalized_at",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m9_teacher_reviews": (
        "decision_id",
        "audit_id",
        "expected_audit_version",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
    "m9_teacher_analytics": (
        "report_id",
        "course_id",
        "class_id",
        "generated_at",
        "learner_ids",
        "payload",
        "payload_checksum",
        "schema_version",
    ),
}
_IDENTITIES = {
    "m0_learning_events": ("event_id",),
    "m0_event_outbox": ("event_id",),
    "m0_assessment_runs": ("operation_id",),
    "m4_task_plans": ("task_id",),
    "m5_learner_states": (
        "course_id",
        "class_id",
        "learner_id",
        "state_version",
    ),
    "m5_class_states": ("course_id", "class_id", "snapshot_id"),
    "m5_state_updates": ("attempt_id", "state_version"),
    "m6_session_states": ("session_id", "turn_count"),
    "m6_policy_artifacts": ("policy_id",),
    "m6_policy_executions": ("request_fingerprint",),
    "m6_tutoring_decisions": ("decision_id",),
    "m6_policy_observations": ("decision_id",),
    "m6_policy_rewards": ("reward_identity",),
    "m6_policy_evaluations": ("evaluation_identity",),
    "m7_student_feedback": ("feedback_id",),
    "m8_assessment_papers": ("paper_id",),
    "m8_score_audits": ("audit_id", "audit_version"),
    "m8_scoring_results": ("attempt_id", "result_key"),
    "m9_teacher_reviews": ("decision_id",),
    "m9_teacher_analytics": ("report_id",),
}
_JSON_COLUMNS = {
    "m0_learning_events": frozenset({"payload"}),
    "m4_task_plans": frozenset({"payload"}),
    "m5_learner_states": frozenset({"payload"}),
    "m5_class_states": frozenset({"payload"}),
    "m5_state_updates": frozenset({"payload"}),
    "m6_session_states": frozenset({"payload"}),
    "m6_policy_artifacts": frozenset({"payload"}),
    "m6_policy_executions": frozenset({"payload"}),
    "m6_tutoring_decisions": frozenset(
        {"evidence_identity", "result_payload"}
    ),
    "m6_policy_observations": frozenset({"payload"}),
    "m6_policy_rewards": frozenset({"payload"}),
    "m6_policy_evaluations": frozenset({"payload"}),
    "m7_student_feedback": frozenset({"payload"}),
    "m8_assessment_papers": frozenset({"payload"}),
    "m8_score_audits": frozenset({"payload"}),
    "m8_scoring_results": frozenset({"payload"}),
    "m9_teacher_reviews": frozenset({"payload"}),
    "m9_teacher_analytics": frozenset({"learner_ids", "payload"}),
}
_TIMESTAMP_COLUMNS = {
    "m0_learning_events": frozenset({"occurred_at"}),
    "m0_event_outbox": frozenset(
        {
            "available_at",
            "locked_at",
            "lease_until",
            "created_at",
            "updated_at",
        }
    ),
    "m0_assessment_runs": frozenset(
        {"lease_until", "created_at", "updated_at"}
    ),
    "m8_scoring_results": frozenset({"finalized_at"}),
    "m9_teacher_analytics": frozenset({"generated_at"}),
}


class PostgresImportDestination:
    """Import exact rows using one transaction for each caller batch."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def apply_batch(self, table: str, rows: tuple[Any, ...]) -> None:
        self._validate_batch(table, rows)
        if not rows:
            return
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    for row in rows:
                        connection.execute(
                            _insert_statement(table),
                            _bound_values(table, row),
                        )
                    _verify_on_connection(connection, table, rows)
        except _migration_error_type():
            raise
        except (PostgresError, psycopg.Error):
            raise _migration_error_type()(
                "TARGET_WRITE_FAILED"
            ) from None

    def verify_batch(self, table: str, rows: tuple[Any, ...]) -> None:
        self._validate_batch(table, rows)
        if not rows:
            return
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _verify_on_connection(connection, table, rows)
        except _migration_error_type():
            raise
        except (PostgresError, psycopg.Error):
            raise _migration_error_type()(
                "TARGET_VERIFICATION_FAILED"
            ) from None

    @staticmethod
    def _validate_batch(table: str, rows: tuple[Any, ...]) -> None:
        if table not in _COLUMNS:
            raise ValueError("table is outside the migration allowlist")
        for row in rows:
            if row.table != table or row.columns != _COLUMNS[table]:
                raise ValueError("row columns are outside the migration allowlist")


def _insert_statement(table: str) -> str:
    columns = _COLUMNS[table]
    identities = _IDENTITIES[table]
    placeholders = ", ".join("%s" for _ in columns)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(identities)}) DO NOTHING"
    )


def _select_statement(table: str) -> str:
    columns = _COLUMNS[table]
    where = " AND ".join(f"{column} = %s" for column in _IDENTITIES[table])
    return f"SELECT {', '.join(columns)} FROM {table} WHERE {where}"


def _bound_values(table: str, row: Any) -> tuple[Any, ...]:
    json_columns = _JSON_COLUMNS.get(table, frozenset())
    timestamp_columns = _TIMESTAMP_COLUMNS.get(table, frozenset())
    return tuple(
        Jsonb(_json_document(value))
        if column in json_columns
        else _timestamp(value)
        if column in timestamp_columns and value is not None
        else value
        for column, value in zip(row.columns, row.values, strict=True)
    )


def _verify_on_connection(
    connection: Any,
    table: str,
    rows: tuple[Any, ...],
) -> None:
    for expected in rows:
        selected = connection.execute(
            _select_statement(table),
            expected.identity,
        ).fetchone()
        if selected is None or not _same_row(table, expected, selected):
            raise _migration_error_type()(
                "TARGET_VERIFICATION_FAILED"
            )


def _same_row(table: str, expected: Any, selected: Mapping[str, Any]) -> bool:
    json_columns = _JSON_COLUMNS.get(table, frozenset())
    timestamp_columns = _TIMESTAMP_COLUMNS.get(table, frozenset())
    for column, expected_value in zip(
        expected.columns,
        expected.values,
        strict=True,
    ):
        actual_value = selected.get(column)
        if column in json_columns:
            if _json_document(expected_value) != _json_document(actual_value):
                return False
        elif column in timestamp_columns:
            if expected_value is None:
                if actual_value is not None:
                    return False
            elif _timestamp(expected_value) != _timestamp(actual_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _json_document(value: Any) -> Any:
    decoded = json.loads(value) if type(value) is str else value
    if type(decoded) not in {dict, list}:
        raise ValueError("JSON import values must be objects or arrays")
    dumps_json(decoded)
    return decoded


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        parsed = datetime.fromisoformat(value)
    else:
        raise ValueError("timestamp import value is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp import value must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _migration_error_type() -> type[RuntimeError]:
    # Delayed import keeps the public module free of a destination cycle.
    from course_insight.infrastructure.postgresql.sqlite_import import (
        MigrationError,
    )

    return MigrationError


__all__ = ["PostgresImportDestination"]
