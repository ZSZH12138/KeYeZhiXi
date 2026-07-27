"""SQLite persistence for replay-safe M6 tutoring decisions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from course_insight.contracts.errors import DomainError
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.modules.m6_tutoring_fsm.identity import EvidenceIdentity
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


_PolicyRecord = TypeVar(
    "_PolicyRecord",
    PolicyArtifactManifest,
    PolicyExecutionRef,
    PolicyObservation,
    PolicyRewardRecord,
    PolicyEvaluationRecord,
)


class SQLiteM6Repository:
    """Persist M6 snapshots and decisions in explicit immediate transactions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        """Create the database and apply every pending migration."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        """Insert one immutable snapshot without overwriting existing history."""

        candidate = isolated_session_snapshot(snapshot)
        payload = dumps_json(candidate.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT session_id, turn_count, payload
                FROM m6_session_states
                WHERE session_id = ? AND turn_count = ?
                """,
                (candidate.session_id, candidate.turn_count),
            ).fetchone()
            if existing is not None:
                assert_same_session_snapshot(_snapshot_from_row(existing), candidate)
                connection.execute("COMMIT")
                return
            latest_row = _latest_snapshot_row(connection, candidate.session_id)
            validate_session_append(
                None if latest_row is None else _snapshot_from_row(latest_row),
                candidate,
            )
            _insert_snapshot(connection, candidate, payload)
            row = connection.execute(
                """
                SELECT session_id, turn_count, payload
                FROM m6_session_states
                WHERE session_id = ? AND turn_count = ?
                """,
                (candidate.session_id, candidate.turn_count),
            ).fetchone()
            if row is None:
                raise RuntimeError("M6 snapshot insert produced no authoritative row")
            assert_same_session_snapshot(_snapshot_from_row(row), candidate)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        """Load one exact session turn as an isolated contract object."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT session_id, turn_count, payload
                FROM m6_session_states
                WHERE session_id = ? AND turn_count = ?
                """,
                (session_id, turn_count),
            ).fetchone()
            return None if row is None else _snapshot_from_row(row)
        finally:
            connection.close()

    def get_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        """Load the greatest persisted turn for one session."""

        connection = connect_sqlite(self._database_path)
        try:
            row = _latest_snapshot_row(connection, session_id)
            return None if row is None else _snapshot_from_row(row)
        finally:
            connection.close()

    def get_decision_by_request(
        self,
        request_fingerprint: str,
    ) -> TutoringDecisionRecord | None:
        """Load one prior exact public request result."""

        connection = connect_sqlite(self._database_path)
        try:
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
        finally:
            connection.close()

    def get_latest_decision(
        self,
        session_id: str,
    ) -> TutoringDecisionRecord | None:
        """Load the greatest evidence-bearing decision for one session."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                f"""
                SELECT {_DECISION_COLUMNS}
                FROM m6_tutoring_decisions
                WHERE session_id = ?
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
        finally:
            connection.close()

    def save_policy_artifact(
        self,
        manifest: PolicyArtifactManifest,
    ) -> PolicyArtifactManifest:
        """Insert or verify one immutable policy artifact manifest."""

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
                manifest.canonical_json(),
                manifest.identity,
            ),
            record_type=PolicyArtifactManifest,
            candidate=manifest,
        )

    def get_policy_artifact(
        self,
        policy_id: str,
    ) -> PolicyArtifactManifest | None:
        """Load one exact policy manifest by its governed policy ID."""

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
        """Load one immutable first-writer binding by public request."""

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
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _policy_row(
                connection,
                "m6_policy_executions",
                ("request_fingerprint",),
                (execution.request_fingerprint,),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO m6_policy_executions(
                        request_fingerprint,
                        policy_execution_fingerprint,
                        payload,
                        payload_checksum
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        execution.request_fingerprint,
                        execution.policy_execution_fingerprint,
                        execution.canonical_json(),
                        execution.identity,
                    ),
                )
                existing = _policy_row(
                    connection,
                    "m6_policy_executions",
                    ("request_fingerprint",),
                    (execution.request_fingerprint,),
                )
            if existing is None:
                raise RuntimeError("M6 policy execution insert produced no row")
            authoritative = _policy_record_from_row(
                existing,
                PolicyExecutionRef,
            )
            connection.execute("COMMIT")
            return authoritative
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_policy_observation(
        self,
        decision_id: str,
    ) -> PolicyObservation | None:
        """Load the private policy observation for one decision."""

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
        """Insert or verify one immutable reward version."""

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
                reward.canonical_json(),
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
        """Load one reward version for a policy execution."""

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
        """Insert or verify one offline evaluation dataset result."""

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
                evaluation.canonical_json(),
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
        """Load one offline evaluation by policy and dataset identity."""

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
        record_type: type[_PolicyRecord],
    ) -> _PolicyRecord | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = _policy_row(connection, table, key_columns, key_values)
            return (
                None
                if row is None
                else _policy_record_from_row(row, record_type)
            )
        finally:
            connection.close()

    def _save_policy_record(
        self,
        *,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[object, ...],
        insert_columns: tuple[str, ...],
        insert_values: tuple[object, ...],
        record_type: type[_PolicyRecord],
        candidate: _PolicyRecord,
    ) -> _PolicyRecord:
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _policy_row(
                connection,
                table,
                key_columns,
                key_values,
            )
            if existing is None:
                _insert_policy_row(
                    connection,
                    table,
                    insert_columns,
                    insert_values,
                )
                existing = _policy_row(
                    connection,
                    table,
                    key_columns,
                    key_values,
                )
            if existing is None:
                raise RuntimeError("M6 policy record insert produced no row")
            stored = _policy_record_from_row(existing, record_type)
            if stored != candidate:
                _raise_policy_integrity_error("policy_record_identity_conflict")
            connection.execute("COMMIT")
            return stored
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def commit_decision(
        self,
        record: TutoringDecisionRecord,
        expected_previous_snapshot: SessionStateSnapshot,
    ) -> TutoringDecisionRecord:
        """Atomically seed history, advance one turn, and store its decision."""

        candidate = record.isolated_copy()
        expected = isolated_session_snapshot(expected_previous_snapshot)
        validate_decision_commit(candidate, expected)
        result_snapshot = candidate.result.session_state_snapshot
        expected_payload = dumps_json(expected.to_dict())
        result_payload = dumps_json(result_snapshot.to_dict())
        evidence_payload = dumps_json(candidate.evidence_identity.to_dict())
        decision_payload = dumps_json(candidate.result.to_dict())

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = _decision_row(
                connection,
                "request_fingerprint",
                candidate.request_fingerprint,
            )
            if replay is None:
                replay = _decision_row(
                    connection,
                    "input_fingerprint",
                    candidate.input_fingerprint,
                )
            if replay is not None:
                authoritative = _decision_with_policy(connection, replay)
                connection.execute("COMMIT")
                return authoritative

            latest_row = _latest_snapshot_row(connection, candidate.session_id)
            if latest_row is None:
                _insert_snapshot(connection, expected, expected_payload)
            else:
                assert_same_session_snapshot(
                    _snapshot_from_row(latest_row),
                    expected,
                )

            _insert_snapshot(connection, result_snapshot, result_payload)
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
                    result_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.decision_id,
                    candidate.session_id,
                    candidate.turn_count,
                    candidate.previous_turn_count,
                    candidate.request_fingerprint,
                    candidate.input_fingerprint,
                    candidate.evidence_fingerprint,
                    evidence_payload,
                    decision_payload,
                ),
            )
            if candidate.policy_observation is not None:
                observation = candidate.policy_observation
                execution = candidate.policy_execution_ref
                assert execution is not None
                persisted_execution = _policy_row(
                    connection,
                    "m6_policy_executions",
                    ("request_fingerprint",),
                    (candidate.request_fingerprint,),
                )
                if persisted_execution is None or _policy_record_from_row(
                    persisted_execution,
                    PolicyExecutionRef,
                ) != execution:
                    _raise_policy_integrity_error(
                        "decision_policy_execution_mismatch"
                    )
                connection.execute(
                    """
                    INSERT INTO m6_policy_observations(
                        decision_id,
                        request_fingerprint,
                        policy_execution_fingerprint,
                        payload,
                        payload_checksum
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.decision_id,
                        candidate.request_fingerprint,
                        execution.policy_execution_fingerprint,
                        observation.canonical_json(),
                        observation.identity,
                    ),
                )
            stored_row = _decision_row(
                connection,
                "request_fingerprint",
                candidate.request_fingerprint,
            )
            if stored_row is None:
                raise RuntimeError("M6 decision insert produced no authoritative row")
            stored = _decision_with_policy(connection, stored_row)
            _assert_same_decision(stored, candidate)
            connection.execute("COMMIT")
            return stored
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


_DECISION_COLUMNS = """
decision_id,
session_id,
turn_count,
previous_turn_count,
request_fingerprint,
input_fingerprint,
evidence_fingerprint,
evidence_identity,
result_payload
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


def _latest_snapshot_row(
    connection: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT session_id, turn_count, payload
        FROM m6_session_states
        WHERE session_id = ?
        ORDER BY turn_count DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def _decision_row(
    connection: sqlite3.Connection,
    column: str,
    value: str,
) -> sqlite3.Row | None:
    if column not in _DECISION_LOOKUP_COLUMNS:
        raise ValueError("unsupported M6 decision lookup column")
    return connection.execute(
        f"""
        SELECT {_DECISION_COLUMNS}
        FROM m6_tutoring_decisions
        WHERE {column} = ?
        """,
        (value,),
    ).fetchone()


def _policy_row(
    connection: sqlite3.Connection,
    table: str,
    key_columns: tuple[str, ...],
    key_values: tuple[object, ...],
) -> sqlite3.Row | None:
    allowed_columns = _POLICY_TABLE_COLUMNS.get(table)
    if (
        allowed_columns is None
        or not key_columns
        or frozenset(key_columns) != allowed_columns
        or len(key_columns) != len(key_values)
    ):
        raise ValueError("unsupported M6 policy lookup")
    predicate = " AND ".join(f"{column} = ?" for column in key_columns)
    return connection.execute(
        f"SELECT * FROM {table} WHERE {predicate}",
        key_values,
    ).fetchone()


def _insert_policy_row(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    if table not in _POLICY_TABLE_COLUMNS or len(columns) != len(values):
        raise ValueError("unsupported M6 policy insert")
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders})",
        values,
    )


def _insert_snapshot(
    connection: sqlite3.Connection,
    snapshot: SessionStateSnapshot,
    payload: str,
) -> None:
    connection.execute(
        """
        INSERT INTO m6_session_states(session_id, turn_count, payload)
        VALUES (?, ?, ?)
        """,
        (snapshot.session_id, snapshot.turn_count, payload),
    )


def _snapshot_from_row(row: sqlite3.Row) -> SessionStateSnapshot:
    payload = str(row["payload"])
    _require_canonical_json(payload, "session snapshot")
    try:
        snapshot = SessionStateSnapshot.model_validate_json(payload)
    except Exception as error:
        raise RuntimeError("M6 session snapshot payload is invalid") from error
    if (
        snapshot.session_id != str(row["session_id"])
        or snapshot.turn_count != int(row["turn_count"])
    ):
        raise RuntimeError("M6 session snapshot columns do not match payload")
    return snapshot


def _decision_from_row(row: sqlite3.Row) -> TutoringDecisionRecord:
    evidence_payload = str(row["evidence_identity"])
    result_payload = str(row["result_payload"])
    _require_canonical_json(evidence_payload, "evidence identity")
    _require_canonical_json(result_payload, "tutoring result")
    try:
        evidence_value = json.loads(evidence_payload)
        if type(evidence_value) is not dict:
            raise ValueError("evidence identity must be an object")
        evidence_identity = EvidenceIdentity.from_dict(evidence_value)
        result = TutoringControlResult.model_validate_json(result_payload)
        previous_value = row["previous_turn_count"]
        record = TutoringDecisionRecord(
            decision_id=str(row["decision_id"]),
            session_id=str(row["session_id"]),
            turn_count=int(row["turn_count"]),
            previous_turn_count=(
                None if previous_value is None else int(previous_value)
            ),
            request_fingerprint=str(row["request_fingerprint"]),
            input_fingerprint=str(row["input_fingerprint"]),
            evidence_identity=evidence_identity,
            result=result,
        )
    except Exception as error:
        raise RuntimeError("M6 tutoring decision payload is invalid") from error
    if record.evidence_fingerprint != str(row["evidence_fingerprint"]):
        raise RuntimeError("M6 evidence fingerprint does not match its payload")
    return record


def _decision_with_policy(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
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
        observation.request_fingerprint != record.request_fingerprint
        or observation.policy_execution_fingerprint
        != execution.policy_execution_fingerprint
    ):
        _raise_policy_integrity_error("policy_observation_identity_mismatch")
    return replace(
        record,
        policy_execution_ref=execution,
        policy_observation=observation,
    )


def _policy_record_from_row(
    row: sqlite3.Row,
    record_type: type[_PolicyRecord],
) -> _PolicyRecord:
    payload = str(row["payload"])
    _require_canonical_json(payload, "policy record")
    try:
        value = json.loads(payload)
        if type(value) is not dict:
            raise ValueError("policy record must be an object")
        if record_type is PolicyArtifactManifest:
            value["allowed_scopes"] = tuple(value["allowed_scopes"])
        elif record_type is PolicyObservation:
            value["candidate_ids"] = tuple(value["candidate_ids"])
        record = record_type(**value)
    except Exception:
        _raise_policy_integrity_error("policy_payload_invalid")
    if (
        record.canonical_json() != payload
        or record.identity != str(row["payload_checksum"])
    ):
        _raise_policy_integrity_error("policy_payload_checksum_mismatch")
    _validate_policy_row_identity(row, record)
    return record


def _validate_policy_row_identity(
    row: sqlite3.Row,
    record: _PolicyRecord,
) -> None:
    keys = set(row.keys())
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
    if any(column in keys and row[column] != expected for column, expected in checks):
        _raise_policy_integrity_error("policy_columns_payload_mismatch")


def _raise_policy_integrity_error(reason: str) -> None:
    raise DomainError(
        code="TUTORING_POLICY_INTEGRITY_ERROR",
        module="m6",
        message="stored tutoring policy data is inconsistent",
        details={"reason": reason},
    )


def _require_canonical_json(payload: str, entity: str) -> None:
    try:
        value: Any = json.loads(payload)
        canonical = dumps_json(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"M6 {entity} JSON is invalid") from error
    if canonical != payload:
        raise RuntimeError(f"M6 {entity} JSON is not canonical")


def _assert_same_decision(
    stored: TutoringDecisionRecord,
    candidate: TutoringDecisionRecord,
) -> None:
    if (
        stored.decision_id != candidate.decision_id
        or stored.session_id != candidate.session_id
        or stored.turn_count != candidate.turn_count
        or stored.previous_turn_count != candidate.previous_turn_count
        or stored.request_fingerprint != candidate.request_fingerprint
        or stored.input_fingerprint != candidate.input_fingerprint
        or stored.evidence_fingerprint != candidate.evidence_fingerprint
        or stored.result.content_checksum() != candidate.result.content_checksum()
    ):
        raise RuntimeError("M6 stored decision does not match its candidate")
