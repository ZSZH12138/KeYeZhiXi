"""PostgreSQL persistence for replay-safe M6 tutoring decisions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from course_insight.contracts.errors import DomainError
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.infrastructure.postgresql.actor_erasure import purge_postgres_actor
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    derive_identifier,
)
from course_insight.modules.m6_tutoring_fsm.repository import (
    TutoringDecisionRecord,
    assert_same_session_snapshot,
    isolated_session_snapshot,
    validate_decision_commit,
    validate_session_append,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
    PolicyEvaluationRecord,
    PolicyExecutionRef,
    PolicyObservation,
    PolicyRewardRecord,
)


_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M6 repository integrity check failed"
_SNAPSHOT_COLUMNS = """
session_id,
turn_count,
payload,
payload_checksum,
schema_version
"""
_DECISION_COLUMNS = """
decision_id,
session_id,
turn_count,
previous_turn_count,
request_fingerprint,
input_fingerprint,
evidence_fingerprint,
evidence_identity,
result_payload,
payload_checksum,
schema_version
"""
_DECISION_LOOKUP_COLUMNS = frozenset(
    {"request_fingerprint", "input_fingerprint"}
)
_POLICY_TABLE_COLUMNS = {
    "m6_policy_artifacts": frozenset({"policy_id"}),
    "m6_policy_executions": frozenset({"request_fingerprint"}),
    "m6_policy_observations": frozenset({"decision_id"}),
    "m6_policy_rewards": frozenset(
        {"policy_execution_fingerprint", "reward_version"}
    ),
    "m6_policy_evaluations": frozenset({"policy_id", "dataset_identity"}),
}


class PostgresM6Repository:
    """Persist append-only M6 histories and atomic tutoring decisions."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def purge_actor(self, actor_id: str) -> int:
        return purge_postgres_actor(self._pool, module="m6", actor_id=actor_id)

    def initialize(self) -> None:
        """Apply every verified PostgreSQL migration."""

        from course_insight.infrastructure.postgresql.migration_runner import (
            run_migrations,
        )

        run_migrations(self._pool)

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        """Insert one immutable snapshot without overwriting history."""

        candidate = isolated_session_snapshot(snapshot)
        _require_current_contract_schemas(candidate)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _lock_session(connection, candidate.session_id)
                    existing_row = _snapshot_row(
                        connection,
                        candidate.session_id,
                        candidate.turn_count,
                    )
                    if existing_row is not None:
                        assert_same_session_snapshot(
                            _snapshot_from_row(existing_row),
                            candidate,
                        )
                        return
                    latest_row = _latest_snapshot_row(
                        connection,
                        candidate.session_id,
                    )
                    validate_session_append(
                        (
                            None
                            if latest_row is None
                            else _snapshot_from_row(latest_row)
                        ),
                        candidate,
                    )
                    _insert_snapshot(connection, candidate)
                    stored_row = _snapshot_row(
                        connection,
                        candidate.session_id,
                        candidate.turn_count,
                    )
                    if stored_row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    assert_same_session_snapshot(
                        _snapshot_from_row(stored_row),
                        candidate,
                    )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        """Load one exact session turn as an isolated contract object."""

        try:
            with self._pool.connection() as connection:
                row = _snapshot_row(connection, session_id, turn_count)
                return None if row is None else _snapshot_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        """Load the greatest persisted turn for one session."""

        try:
            with self._pool.connection() as connection:
                row = _latest_snapshot_row(connection, session_id)
                return None if row is None else _snapshot_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_decision_by_request(
        self,
        request_fingerprint: str,
    ) -> TutoringDecisionRecord | None:
        """Load one exact public-request replay, if present."""

        try:
            with self._pool.connection() as connection:
                row = _decision_row(
                    connection,
                    "request_fingerprint",
                    request_fingerprint,
                )
                return (
                    None
                    if row is None
                    else _decision_with_policy(connection, row)
                )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_decision(
        self,
        session_id: str,
    ) -> TutoringDecisionRecord | None:
        """Load the greatest evidence-bearing decision for one session."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_DECISION_COLUMNS}
                    FROM m6_tutoring_decisions
                    WHERE session_id = %s
                    ORDER BY turn_count DESC
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                return (
                    None
                    if row is None
                    else _decision_with_policy(connection, row)
                )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def commit_decision(
        self,
        record: TutoringDecisionRecord,
        expected_previous_snapshot: SessionStateSnapshot,
    ) -> TutoringDecisionRecord:
        """Atomically seed history, advance one turn, and store a decision."""

        if not isinstance(record, TutoringDecisionRecord):
            raise TypeError("record must be a TutoringDecisionRecord")
        candidate = record.isolated_copy()
        expected = isolated_session_snapshot(expected_previous_snapshot)
        _require_current_contract_schemas(candidate.result)
        _require_current_contract_schemas(expected)
        validate_decision_commit(candidate, expected)
        result_snapshot = candidate.result.session_state_snapshot

        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _lock_session(connection, candidate.session_id)
                    request_row = _decision_row(
                        connection,
                        "request_fingerprint",
                        candidate.request_fingerprint,
                    )
                    if request_row is not None:
                        authoritative = _decision_with_policy(
                            connection,
                            request_row,
                        )
                        _assert_replay_compatible(
                            authoritative,
                            candidate,
                            allow_request_mismatch=False,
                        )
                        return authoritative
                    input_row = _decision_row(
                        connection,
                        "input_fingerprint",
                        candidate.input_fingerprint,
                    )
                    if input_row is not None:
                        authoritative = _decision_with_policy(
                            connection,
                            input_row,
                        )
                        _assert_replay_compatible(
                            authoritative,
                            candidate,
                            allow_request_mismatch=True,
                        )
                        return authoritative

                    latest_row = _latest_snapshot_row(
                        connection,
                        candidate.session_id,
                    )
                    if latest_row is None:
                        _insert_snapshot(connection, expected)
                    else:
                        assert_same_session_snapshot(
                            _snapshot_from_row(latest_row),
                            expected,
                        )
                    _insert_snapshot(connection, result_snapshot)
                    _insert_decision(connection, candidate)
                    if candidate.policy_observation is not None:
                        _insert_policy_observation(connection, candidate)

                    stored_snapshot_row = _snapshot_row(
                        connection,
                        candidate.session_id,
                        candidate.turn_count,
                    )
                    if stored_snapshot_row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    assert_same_session_snapshot(
                        _snapshot_from_row(stored_snapshot_row),
                        result_snapshot,
                    )
                    stored_row = _decision_row(
                        connection,
                        "request_fingerprint",
                        candidate.request_fingerprint,
                    )
                    if stored_row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    stored = _decision_with_policy(connection, stored_row)
                    _assert_replay_compatible(
                        stored,
                        candidate,
                        allow_request_mismatch=False,
                    )
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_policy_artifact(
        self,
        manifest: PolicyArtifactManifest,
    ) -> PolicyArtifactManifest:
        """Insert or verify one immutable artifact manifest."""

        if not isinstance(manifest, PolicyArtifactManifest):
            raise TypeError("manifest must be a PolicyArtifactManifest")
        return self._save_policy_record(
            table="m6_policy_artifacts",
            key_columns=("policy_id",),
            key_values=(manifest.policy_id,),
            insert_columns=(
                "policy_id",
                "artifact_sha256",
                "payload",
                "payload_checksum",
            ),
            insert_values=(
                manifest.policy_id,
                manifest.artifact_sha256,
                Jsonb(dict(manifest.canonical_payload())),
                manifest.identity,
            ),
            record_type=PolicyArtifactManifest,
            candidate=manifest,
        )

    def get_policy_artifact(
        self,
        policy_id: str,
    ) -> PolicyArtifactManifest | None:
        return self._get_policy_record(
            table="m6_policy_artifacts",
            key_columns=("policy_id",),
            key_values=(policy_id,),
            record_type=PolicyArtifactManifest,
        )

    def get_policy_execution_by_request(
        self,
        request_fingerprint: str,
    ) -> PolicyExecutionRef | None:
        return self._get_policy_record(
            table="m6_policy_executions",
            key_columns=("request_fingerprint",),
            key_values=(request_fingerprint,),
            record_type=PolicyExecutionRef,
        )

    def commit_policy_execution(
        self,
        execution: PolicyExecutionRef,
    ) -> PolicyExecutionRef:
        """Insert or return the first policy binding for one request."""

        if not isinstance(execution, PolicyExecutionRef):
            raise TypeError("execution must be a PolicyExecutionRef")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _lock_session(
                        connection,
                        f"policy-request:{execution.request_fingerprint}",
                    )
                    row = _policy_row(
                        connection,
                        "m6_policy_executions",
                        ("request_fingerprint",),
                        (execution.request_fingerprint,),
                    )
                    if row is None:
                        connection.execute(
                            """
                            INSERT INTO m6_policy_executions(
                                request_fingerprint,
                                policy_execution_fingerprint,
                                payload,
                                payload_checksum
                            ) VALUES (%s, %s, %s, %s)
                            """,
                            (
                                execution.request_fingerprint,
                                execution.policy_execution_fingerprint,
                                Jsonb(dict(execution.canonical_payload())),
                                execution.identity,
                            ),
                        )
                        row = _policy_row(
                            connection,
                            "m6_policy_executions",
                            ("request_fingerprint",),
                            (execution.request_fingerprint,),
                        )
                    if row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    return _policy_record_from_row(row, PolicyExecutionRef)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_policy_observation(
        self,
        decision_id: str,
    ) -> PolicyObservation | None:
        return self._get_policy_record(
            table="m6_policy_observations",
            key_columns=("decision_id",),
            key_values=(decision_id,),
            record_type=PolicyObservation,
        )

    def save_policy_reward(
        self,
        reward: PolicyRewardRecord,
    ) -> PolicyRewardRecord:
        if not isinstance(reward, PolicyRewardRecord):
            raise TypeError("reward must be a PolicyRewardRecord")
        return self._save_policy_record(
            table="m6_policy_rewards",
            key_columns=("policy_execution_fingerprint", "reward_version"),
            key_values=(
                reward.policy_execution_fingerprint,
                reward.reward_version,
            ),
            insert_columns=(
                "reward_identity",
                "policy_execution_fingerprint",
                "reward_version",
                "payload",
                "payload_checksum",
            ),
            insert_values=(
                reward.identity,
                reward.policy_execution_fingerprint,
                reward.reward_version,
                Jsonb(dict(reward.canonical_payload())),
                reward.identity,
            ),
            record_type=PolicyRewardRecord,
            candidate=reward,
        )

    def get_policy_reward(
        self,
        policy_execution_fingerprint: str,
        reward_version: str = "m6-reward-v1",
    ) -> PolicyRewardRecord | None:
        return self._get_policy_record(
            table="m6_policy_rewards",
            key_columns=("policy_execution_fingerprint", "reward_version"),
            key_values=(policy_execution_fingerprint, reward_version),
            record_type=PolicyRewardRecord,
        )

    def save_policy_evaluation(
        self,
        evaluation: PolicyEvaluationRecord,
    ) -> PolicyEvaluationRecord:
        if not isinstance(evaluation, PolicyEvaluationRecord):
            raise TypeError("evaluation must be a PolicyEvaluationRecord")
        return self._save_policy_record(
            table="m6_policy_evaluations",
            key_columns=("policy_id", "dataset_identity"),
            key_values=(evaluation.policy_id, evaluation.dataset_identity),
            insert_columns=(
                "evaluation_identity",
                "policy_id",
                "dataset_identity",
                "payload",
                "payload_checksum",
            ),
            insert_values=(
                evaluation.identity,
                evaluation.policy_id,
                evaluation.dataset_identity,
                Jsonb(dict(evaluation.canonical_payload())),
                evaluation.identity,
            ),
            record_type=PolicyEvaluationRecord,
            candidate=evaluation,
        )

    def get_policy_evaluation(
        self,
        policy_id: str,
        dataset_identity: str,
    ) -> PolicyEvaluationRecord | None:
        return self._get_policy_record(
            table="m6_policy_evaluations",
            key_columns=("policy_id", "dataset_identity"),
            key_values=(policy_id, dataset_identity),
            record_type=PolicyEvaluationRecord,
        )

    def _get_policy_record(
        self,
        *,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[object, ...],
        record_type: type[Any],
    ) -> Any:
        try:
            with self._pool.connection() as connection:
                row = _policy_row(
                    connection,
                    table,
                    key_columns,
                    key_values,
                )
                return (
                    None
                    if row is None
                    else _policy_record_from_row(row, record_type)
                )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def _save_policy_record(
        self,
        *,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[object, ...],
        insert_columns: tuple[str, ...],
        insert_values: tuple[object, ...],
        record_type: type[Any],
        candidate: Any,
    ) -> Any:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _lock_session(
                        connection,
                        "policy-record:" + ":".join(map(str, key_values)),
                    )
                    row = _policy_row(
                        connection,
                        table,
                        key_columns,
                        key_values,
                    )
                    if row is None:
                        _insert_policy_row(
                            connection,
                            table,
                            insert_columns,
                            insert_values,
                        )
                        row = _policy_row(
                            connection,
                            table,
                            key_columns,
                            key_values,
                        )
                    if row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    stored = _policy_record_from_row(row, record_type)
                    if stored != candidate:
                        _raise_policy_integrity_error(
                            "policy_record_identity_conflict"
                        )
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _lock_session(connection: Any, session_id: str) -> None:
    digest = hashlib.sha256(
        f"course-insight:m6:{session_id}".encode("utf-8")
    ).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (lock_key,),
    )


def _snapshot_row(
    connection: Any,
    session_id: str,
    turn_count: int,
) -> Any:
    return connection.execute(
        f"""
        SELECT {_SNAPSHOT_COLUMNS}
        FROM m6_session_states
        WHERE session_id = %s AND turn_count = %s
        """,
        (session_id, turn_count),
    ).fetchone()


def _latest_snapshot_row(connection: Any, session_id: str) -> Any:
    return connection.execute(
        f"""
        SELECT {_SNAPSHOT_COLUMNS}
        FROM m6_session_states
        WHERE session_id = %s
        ORDER BY turn_count DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def _decision_row(connection: Any, column: str, value: str) -> Any:
    if column not in _DECISION_LOOKUP_COLUMNS:
        raise ValueError("unsupported M6 decision lookup column")
    return connection.execute(
        f"""
        SELECT {_DECISION_COLUMNS}
        FROM m6_tutoring_decisions
        WHERE {column} = %s
        """,
        (value,),
    ).fetchone()


def _policy_row(
    connection: Any,
    table: str,
    key_columns: tuple[str, ...],
    key_values: tuple[object, ...],
) -> Any:
    allowed_columns = _POLICY_TABLE_COLUMNS.get(table)
    if (
        allowed_columns is None
        or not key_columns
        or frozenset(key_columns) != allowed_columns
        or len(key_columns) != len(key_values)
    ):
        raise ValueError("unsupported M6 policy lookup")
    predicate = " AND ".join(f"{column} = %s" for column in key_columns)
    return connection.execute(
        f"SELECT * FROM {table} WHERE {predicate}",
        key_values,
    ).fetchone()


def _insert_policy_row(
    connection: Any,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    if table not in _POLICY_TABLE_COLUMNS or len(columns) != len(values):
        raise ValueError("unsupported M6 policy insert")
    placeholders = ", ".join("%s" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders})",
        values,
    )


def _insert_snapshot(
    connection: Any,
    snapshot: SessionStateSnapshot,
) -> None:
    connection.execute(
        """
        INSERT INTO m6_session_states(
            session_id,
            turn_count,
            payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            snapshot.session_id,
            snapshot.turn_count,
            Jsonb(snapshot.to_dict()),
            snapshot.content_checksum(),
            snapshot.schema_version,
        ),
    )


def _insert_decision(
    connection: Any,
    record: TutoringDecisionRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO m6_tutoring_decisions(
            decision_id,
            session_id,
            turn_count,
            previous_turn_count,
            request_fingerprint,
            input_fingerprint,
            evidence_fingerprint,
            evidence_identity,
            result_payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            record.decision_id,
            record.session_id,
            record.turn_count,
            record.previous_turn_count,
            record.request_fingerprint,
            record.input_fingerprint,
            record.evidence_fingerprint,
            Jsonb(record.evidence_identity.to_dict()),
            Jsonb(record.result.to_dict()),
            record.result.content_checksum(),
            record.result.schema_version,
        ),
    )


def _insert_policy_observation(
    connection: Any,
    record: TutoringDecisionRecord,
) -> None:
    observation = record.policy_observation
    execution = record.policy_execution_ref
    if observation is None or execution is None:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    execution_row = _policy_row(
        connection,
        "m6_policy_executions",
        ("request_fingerprint",),
        (record.request_fingerprint,),
    )
    if execution_row is None or _policy_record_from_row(
        execution_row,
        PolicyExecutionRef,
    ) != execution:
        _raise_policy_integrity_error("decision_policy_execution_mismatch")
    connection.execute(
        """
        INSERT INTO m6_policy_observations(
            decision_id,
            request_fingerprint,
            policy_execution_fingerprint,
            payload,
            payload_checksum
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            record.decision_id,
            record.request_fingerprint,
            execution.policy_execution_fingerprint,
            Jsonb(dict(observation.canonical_payload())),
            observation.identity,
        ),
    )


def _snapshot_from_row(row: Any) -> SessionStateSnapshot:
    try:
        session_id = _required_string(row, "session_id")
        turn_count = _required_integer(row, "turn_count")
        payload = _required_json_object(row, "payload")
        snapshot = SessionStateSnapshot.model_validate(payload)
        _require_current_contract_schemas(snapshot)
        _verify_payload_metadata(snapshot, row)
        if (
            snapshot.session_id != session_id
            or snapshot.turn_count != turn_count
        ):
            raise ValueError("snapshot columns do not match payload")
        return snapshot
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _decision_from_row(row: Any) -> TutoringDecisionRecord:
    try:
        evidence_payload = _required_json_object(row, "evidence_identity")
        result_payload = _required_json_object(row, "result_payload")
        evidence_identity = EvidenceIdentity.from_dict(evidence_payload)
        result = TutoringControlResult.model_validate(result_payload)
        _require_current_contract_schemas(result)
        _verify_payload_metadata(result, row)
        previous_value = row["previous_turn_count"]
        if previous_value is not None and type(previous_value) is not int:
            raise ValueError("previous turn must be an integer or null")
        record = TutoringDecisionRecord(
            decision_id=_required_string(row, "decision_id"),
            session_id=_required_string(row, "session_id"),
            turn_count=_required_integer(row, "turn_count"),
            previous_turn_count=previous_value,
            request_fingerprint=_required_sha256(
                row,
                "request_fingerprint",
            ),
            input_fingerprint=_required_sha256(row, "input_fingerprint"),
            evidence_identity=evidence_identity,
            result=result,
        )
        if (
            record.evidence_fingerprint
            != _required_sha256(row, "evidence_fingerprint")
            or result.teaching_action.action_id
            != derive_identifier("action", record.input_fingerprint)
            or result.evidence_query.query_id
            != derive_identifier("query", record.input_fingerprint)
            or result.feedback_generation_task.feedback_task_id
            != derive_identifier("feedback_task", record.input_fingerprint)
        ):
            raise ValueError("decision identity does not match payload")
        return record
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _decision_with_policy(
    connection: Any,
    row: Any,
) -> TutoringDecisionRecord:
    record = _decision_from_row(row)
    observation_row = _policy_row(
        connection,
        "m6_policy_observations",
        ("decision_id",),
        (record.decision_id,),
    )
    if observation_row is None:
        return record
    observation = _policy_record_from_row(
        observation_row,
        PolicyObservation,
    )
    execution_row = _policy_row(
        connection,
        "m6_policy_executions",
        ("request_fingerprint",),
        (record.request_fingerprint,),
    )
    if execution_row is None:
        _raise_policy_integrity_error("policy_observation_missing_execution")
    execution = _policy_record_from_row(execution_row, PolicyExecutionRef)
    if (
        observation.decision_id not in (None, record.decision_id)
        or observation.request_fingerprint != record.request_fingerprint
        or observation.policy_execution_fingerprint
        != execution.policy_execution_fingerprint
    ):
        _raise_policy_integrity_error("policy_observation_identity_mismatch")
    return replace(
        record,
        policy_execution_ref=execution,
        policy_observation=observation,
    )


def _policy_record_from_row(row: Any, record_type: type[Any]) -> Any:
    try:
        payload = _required_json_object(row, "payload")
        value = dict(payload)
        if record_type is PolicyArtifactManifest:
            allowed_scopes = value.get("allowed_scopes")
            if type(allowed_scopes) is not list:
                raise ValueError("allowed scopes must be a JSON array")
            value["allowed_scopes"] = tuple(allowed_scopes)
        elif record_type is PolicyObservation:
            candidate_ids = value.get("candidate_ids")
            if type(candidate_ids) is not list:
                raise ValueError("candidate IDs must be a JSON array")
            value["candidate_ids"] = tuple(candidate_ids)
        record = record_type(**value)
        if record.identity != _required_sha256(row, "payload_checksum"):
            raise ValueError("policy payload checksum is inconsistent")
        _validate_policy_row_identity(row, record)
        return record
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _validate_policy_row_identity(row: Any, record: Any) -> None:
    checks: tuple[tuple[str, object], ...]
    if isinstance(record, PolicyArtifactManifest):
        checks = (
            ("policy_id", record.policy_id),
            ("artifact_sha256", record.artifact_sha256),
        )
    elif isinstance(record, PolicyExecutionRef):
        checks = (
            ("request_fingerprint", record.request_fingerprint),
            (
                "policy_execution_fingerprint",
                record.policy_execution_fingerprint,
            ),
        )
    elif isinstance(record, PolicyObservation):
        checks = (
            *(
                ()
                if record.decision_id is None
                else (("decision_id", record.decision_id),)
            ),
            ("request_fingerprint", record.request_fingerprint),
            (
                "policy_execution_fingerprint",
                record.policy_execution_fingerprint,
            ),
        )
    elif isinstance(record, PolicyRewardRecord):
        checks = (
            ("reward_identity", record.identity),
            (
                "policy_execution_fingerprint",
                record.policy_execution_fingerprint,
            ),
            ("reward_version", record.reward_version),
        )
    else:
        checks = (
            ("evaluation_identity", record.identity),
            ("policy_id", record.policy_id),
            ("dataset_identity", record.dataset_identity),
        )
    for column, expected in checks:
        if column in row and row[column] != expected:
            raise ValueError("policy columns do not match payload")


def _verify_payload_metadata(model: BaseModel, row: Any) -> None:
    stored_checksum = _required_sha256(row, "payload_checksum")
    stored_schema_version = _required_string(row, "schema_version")
    actual_schema_version = getattr(model, "schema_version", None)
    if (
        actual_schema_version != stored_schema_version
        or model.content_checksum() != stored_checksum
    ):
        raise ValueError("payload metadata is inconsistent")


def _require_current_contract_schemas(value: Any) -> None:
    if isinstance(value, BaseModel):
        field = type(value).model_fields.get("schema_version")
        if field is not None and getattr(value, "schema_version") != field.default:
            raise ValueError("persisted contract schema version is unsupported")
        for field_name in type(value).model_fields:
            _require_current_contract_schemas(getattr(value, field_name))
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            _require_current_contract_schemas(nested)
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for nested in value:
            _require_current_contract_schemas(nested)


def _required_json_object(row: Any, field: str) -> dict[str, Any]:
    value = row[field]
    if type(value) is not dict:
        raise ValueError(f"{field} must be a JSON object")
    return value


def _required_string(row: Any, field: str) -> str:
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _required_integer(row: Any, field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _required_sha256(row: Any, field: str) -> str:
    value = _required_string(row, field)
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _assert_replay_compatible(
    stored: TutoringDecisionRecord,
    candidate: TutoringDecisionRecord,
    *,
    allow_request_mismatch: bool,
) -> None:
    if (
        stored.decision_id != candidate.decision_id
        or stored.session_id != candidate.session_id
        or stored.turn_count != candidate.turn_count
        or stored.previous_turn_count != candidate.previous_turn_count
        or (
            not allow_request_mismatch
            and stored.request_fingerprint != candidate.request_fingerprint
        )
        or stored.input_fingerprint != candidate.input_fingerprint
        or stored.evidence_identity != candidate.evidence_identity
        or stored.evidence_fingerprint != candidate.evidence_fingerprint
        or stored.result.content_checksum()
        != candidate.result.content_checksum()
    ):
        _raise_reference_mismatch("persisted_decision_conflict")


def _raise_reference_mismatch(reason: str) -> None:
    raise DomainError(
        code="TUTORING_REFERENCE_MISMATCH",
        module="m6",
        message="tutoring history conflicts with its persisted authority",
        details={"reason": reason},
    )


def _raise_policy_integrity_error(reason: str) -> None:
    raise DomainError(
        code="TUTORING_POLICY_INTEGRITY_ERROR",
        module="m6",
        message="stored tutoring policy data is inconsistent",
        details={"reason": reason},
    )


PostgreSQLM6Repository = PostgresM6Repository

__all__ = [
    "PostgresM6Repository",
    "PostgresOperationError",
    "PostgreSQLM6Repository",
]
