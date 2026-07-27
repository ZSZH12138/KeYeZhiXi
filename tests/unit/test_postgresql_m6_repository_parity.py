from __future__ import annotations

import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import ModuleType
from typing import Any, Iterator

import pytest
from psycopg.types.json import Jsonb

from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.state import DiagnosisResult, ItemDiagnosis
from course_insight.contracts.tutoring import (
    FeedbackGenerationTask,
    SessionStateSnapshot,
    TeachingAction,
    TutoringControlResult,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
)
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    derive_identifier,
)
from course_insight.modules.m6_tutoring_fsm.repository import (
    TutoringDecisionRecord,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
    PolicyEvaluationRecord,
    PolicyExecutionRef,
    PolicyObservation,
    PolicyRewardRecord,
)


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _repository_module() -> Any:
    try:
        return importlib.import_module(
            "course_insight.infrastructure.postgresql.m6_repository"
        )
    except ImportError as error:
        pytest.fail(f"PostgresM6Repository is unavailable: {error}")


def _snapshot(
    *,
    state: str = "S1",
    turn_count: int = 0,
    action_ids: list[str] | None = None,
    session_id: str = "session_1",
) -> SessionStateSnapshot:
    return SessionStateSnapshot(
        session_id=session_id,
        current_state=state,
        turn_count=turn_count,
        completed_action_ids=list(action_ids or []),
        updated_at=NOW + timedelta(minutes=turn_count),
    )


def _diagnosis() -> DiagnosisResult:
    return DiagnosisResult(
        diagnosis_id="diagnosis_1",
        attempt_id="attempt_1",
        learner_id="pseudonym_learner",
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="item_instance_1",
                concept_ids=["concept_1"],
                misconception_ids=[],
                error_type="none",
                confidence=1.0,
                evidence_audit_ids=["audit_1:1"],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=["concept_1"],
        priority_misconception_ids=[],
        generated_at=NOW,
    )


def _record(
    previous: SessionStateSnapshot,
    *,
    request_fingerprint: str = "a" * 64,
    input_fingerprint: str = "b" * 64,
    created_at: datetime | None = None,
) -> TutoringDecisionRecord:
    next_state = {"S1": "S3", "S3": "S4", "S4": "S3"}[
        previous.current_state
    ]
    action_id = derive_identifier("action", input_fingerprint)
    query_id = derive_identifier("query", input_fingerprint)
    feedback_id = derive_identifier("feedback_task", input_fingerprint)
    action = TeachingAction(
        action_id=action_id,
        state_before=previous.current_state,
        action_type="guided_question",
        target_concept_ids=["concept_1"],
        prompt_template_id="m6.test.v1",
        must_not_reveal_answer=True,
        next_state=next_state,
        reason="deterministic test decision",
    )
    query = EvidenceQuery(
        query_id=query_id,
        course_package_id="package_1",
        query_text="Retrieve governed evidence for safe feedback.",
        concept_ids=["concept_1"],
        item_id=None,
        use_case="feedback",
        top_k=3,
        min_relevance=0.2,
    )
    result_time = created_at or NOW + timedelta(minutes=previous.turn_count + 1)
    feedback = FeedbackGenerationTask(
        feedback_task_id=feedback_id,
        task_id="task_1",
        learner_id="pseudonym_learner",
        teaching_action=action,
        diagnosis_result=_diagnosis(),
        score_summary={"total_score": 1.0, "max_score": 1.0},
        learner_state_snapshot_id="learner_snapshot_1",
        evidence_query_id=query_id,
        created_at=result_time,
    )
    result_snapshot = SessionStateSnapshot(
        session_id=previous.session_id,
        current_state=next_state,
        turn_count=previous.turn_count + 1,
        completed_action_ids=[*previous.completed_action_ids, action_id],
        updated_at=result_time,
    )
    result = TutoringControlResult(
        teaching_action=action,
        feedback_generation_task=feedback,
        evidence_query=query,
        session_state_snapshot=result_snapshot,
        created_at=result_time,
    )
    return TutoringDecisionRecord(
        decision_id=derive_identifier("decision", input_fingerprint),
        session_id=previous.session_id,
        turn_count=result_snapshot.turn_count,
        previous_turn_count=previous.turn_count,
        request_fingerprint=request_fingerprint,
        input_fingerprint=input_fingerprint,
        evidence_identity=EvidenceIdentity(
            scoring_result_checksum="c" * 64,
            latest_audit_version_keys=("audit_1:1",),
            processed_audit_ids=("audit_1:1",),
            learner_state_version=1,
            learner_state_checksum="d" * 64,
        ),
        result=result,
    )


def _policy_execution(
    request_fingerprint: str = "a" * 64,
    *,
    policy_id: str = "policy_rules_v1",
) -> PolicyExecutionRef:
    return PolicyExecutionRef(
        request_fingerprint=request_fingerprint,
        mode="rules",
        policy_id=policy_id,
        adapter_id="rules",
        adapter_version="1",
        artifact_sha256=None,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-actions-v1",
        gate_policy_version="m6-gate-v1",
    )


class _Result:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = deepcopy(row)

    def fetchone(self) -> dict[str, Any] | None:
        return deepcopy(self._row)


class _FakeM6Database:
    def __init__(self) -> None:
        self.lock = RLock()
        self.snapshots: dict[tuple[str, int], dict[str, Any]] = {}
        self.decisions_by_request: dict[str, dict[str, Any]] = {}
        self.decisions_by_input: dict[str, dict[str, Any]] = {}
        self.decisions_by_turn: dict[tuple[str, int], dict[str, Any]] = {}
        self.policy_executions: dict[str, dict[str, Any]] = {}
        self.policy_observations: dict[str, dict[str, Any]] = {}
        self.policy_artifacts: dict[str, dict[str, Any]] = {}
        self.policy_rewards: dict[tuple[str, str], dict[str, Any]] = {}
        self.policy_evaluations: dict[tuple[str, str], dict[str, Any]] = {}
        self.connection_count = 0
        self.transaction_count = 0
        self.advisory_lock_count = 0
        self.jsonb_bind_count = 0
        self.fail_decision_insert = False
        self.executed: list[tuple[str, tuple[Any, ...]]] = []


class _Transaction:
    def __init__(self, database: _FakeM6Database) -> None:
        self._database = database
        self._backup: tuple[Any, ...] | None = None

    def __enter__(self) -> None:
        self._database.lock.acquire()
        self._database.transaction_count += 1
        self._backup = (
            deepcopy(self._database.snapshots),
            deepcopy(self._database.decisions_by_request),
            deepcopy(self._database.decisions_by_input),
            deepcopy(self._database.decisions_by_turn),
            deepcopy(self._database.policy_executions),
            deepcopy(self._database.policy_observations),
            deepcopy(self._database.policy_artifacts),
            deepcopy(self._database.policy_rewards),
            deepcopy(self._database.policy_evaluations),
        )

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: Any,
    ) -> None:
        if error_type is not None and self._backup is not None:
            (
                self._database.snapshots,
                self._database.decisions_by_request,
                self._database.decisions_by_input,
                self._database.decisions_by_turn,
                self._database.policy_executions,
                self._database.policy_observations,
                self._database.policy_artifacts,
                self._database.policy_rewards,
                self._database.policy_evaluations,
            ) = self._backup
        self._database.lock.release()


class _FakeConnection:
    def __init__(self, database: _FakeM6Database) -> None:
        self._database = database

    def transaction(self) -> _Transaction:
        return _Transaction(self._database)

    def execute(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> _Result:
        normalized = " ".join(query.split())
        self._database.executed.append((normalized, parameters))
        if "pg_advisory_xact_lock" in normalized:
            self._database.advisory_lock_count += 1
            return _Result()
        if normalized.startswith("INSERT INTO m6_session_states"):
            session_id, turn_count, payload, checksum, schema_version = parameters
            assert isinstance(payload, Jsonb)
            self._database.jsonb_bind_count += 1
            key = (str(session_id), int(turn_count))
            if key in self._database.snapshots:
                raise AssertionError("unexpected duplicate snapshot insert")
            self._database.snapshots[key] = {
                "session_id": str(session_id),
                "turn_count": int(turn_count),
                "payload": deepcopy(payload.obj),
                "payload_checksum": str(checksum),
                "schema_version": str(schema_version),
            }
            return _Result()
        if (
            "FROM m6_session_states" in normalized
            and "turn_count = %s" in normalized
        ):
            return _Result(
                self._database.snapshots.get(
                    (str(parameters[0]), int(parameters[1]))
                )
            )
        if (
            "FROM m6_session_states" in normalized
            and "ORDER BY turn_count DESC" in normalized
        ):
            session_id = str(parameters[0])
            candidates = [
                row
                for (stored_session_id, _), row in self._database.snapshots.items()
                if stored_session_id == session_id
            ]
            return _Result(
                None
                if not candidates
                else max(candidates, key=lambda row: int(row["turn_count"]))
            )
        if normalized.startswith("INSERT INTO m6_tutoring_decisions"):
            if self._database.fail_decision_insert:
                raise RuntimeError("forced decision insert failure")
            (
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
                schema_version,
            ) = parameters
            assert isinstance(evidence_identity, Jsonb)
            assert isinstance(result_payload, Jsonb)
            self._database.jsonb_bind_count += 2
            row = {
                "decision_id": str(decision_id),
                "session_id": str(session_id),
                "turn_count": int(turn_count),
                "previous_turn_count": (
                    None
                    if previous_turn_count is None
                    else int(previous_turn_count)
                ),
                "request_fingerprint": str(request_fingerprint),
                "input_fingerprint": str(input_fingerprint),
                "evidence_fingerprint": str(evidence_fingerprint),
                "evidence_identity": deepcopy(evidence_identity.obj),
                "result_payload": deepcopy(result_payload.obj),
                "payload_checksum": str(payload_checksum),
                "schema_version": str(schema_version),
            }
            self._database.decisions_by_request[str(request_fingerprint)] = row
            self._database.decisions_by_input[str(input_fingerprint)] = row
            self._database.decisions_by_turn[
                (str(session_id), int(turn_count))
            ] = row
            return _Result()
        if normalized.startswith("INSERT INTO m6_policy_executions"):
            request_fingerprint, execution_fingerprint, payload, checksum = parameters
            assert isinstance(payload, Jsonb)
            self._database.jsonb_bind_count += 1
            self._database.policy_executions[str(request_fingerprint)] = {
                "request_fingerprint": str(request_fingerprint),
                "policy_execution_fingerprint": str(execution_fingerprint),
                "payload": deepcopy(payload.obj),
                "payload_checksum": str(checksum),
            }
            return _Result()
        if (
            "FROM m6_policy_executions" in normalized
            and "request_fingerprint = %s" in normalized
        ):
            return _Result(
                self._database.policy_executions.get(str(parameters[0]))
            )
        if normalized.startswith("INSERT INTO m6_policy_observations"):
            (
                decision_id,
                request_fingerprint,
                execution_fingerprint,
                payload,
                checksum,
            ) = parameters
            assert isinstance(payload, Jsonb)
            self._database.jsonb_bind_count += 1
            self._database.policy_observations[str(decision_id)] = {
                "decision_id": str(decision_id),
                "request_fingerprint": str(request_fingerprint),
                "policy_execution_fingerprint": str(execution_fingerprint),
                "payload": deepcopy(payload.obj),
                "payload_checksum": str(checksum),
            }
            return _Result()
        if (
            "FROM m6_policy_observations" in normalized
            and "decision_id = %s" in normalized
        ):
            return _Result(
                self._database.policy_observations.get(str(parameters[0]))
            )
        if normalized.startswith("INSERT INTO m6_policy_artifacts"):
            policy_id, artifact_sha256, payload, checksum = parameters
            assert isinstance(payload, Jsonb)
            self._database.policy_artifacts[str(policy_id)] = {
                "policy_id": str(policy_id),
                "artifact_sha256": str(artifact_sha256),
                "payload": deepcopy(payload.obj),
                "payload_checksum": str(checksum),
            }
            return _Result()
        if "FROM m6_policy_artifacts" in normalized:
            return _Result(
                self._database.policy_artifacts.get(str(parameters[0]))
            )
        if normalized.startswith("INSERT INTO m6_policy_rewards"):
            identity, execution_id, version, payload, checksum = parameters
            assert isinstance(payload, Jsonb)
            key = (str(execution_id), str(version))
            self._database.policy_rewards[key] = {
                "reward_identity": str(identity),
                "policy_execution_fingerprint": str(execution_id),
                "reward_version": str(version),
                "payload": deepcopy(payload.obj),
                "payload_checksum": str(checksum),
            }
            return _Result()
        if "FROM m6_policy_rewards" in normalized:
            return _Result(
                self._database.policy_rewards.get(
                    (str(parameters[0]), str(parameters[1]))
                )
            )
        if normalized.startswith("INSERT INTO m6_policy_evaluations"):
            identity, policy_id, dataset_id, payload, checksum = parameters
            assert isinstance(payload, Jsonb)
            key = (str(policy_id), str(dataset_id))
            self._database.policy_evaluations[key] = {
                "evaluation_identity": str(identity),
                "policy_id": str(policy_id),
                "dataset_identity": str(dataset_id),
                "payload": deepcopy(payload.obj),
                "payload_checksum": str(checksum),
            }
            return _Result()
        if "FROM m6_policy_evaluations" in normalized:
            return _Result(
                self._database.policy_evaluations.get(
                    (str(parameters[0]), str(parameters[1]))
                )
            )
        if (
            "FROM m6_tutoring_decisions" in normalized
            and "WHERE request_fingerprint = %s" in normalized
        ):
            return _Result(
                self._database.decisions_by_request.get(str(parameters[0]))
            )
        if (
            "FROM m6_tutoring_decisions" in normalized
            and "WHERE input_fingerprint = %s" in normalized
        ):
            return _Result(
                self._database.decisions_by_input.get(str(parameters[0]))
            )
        if (
            "FROM m6_tutoring_decisions" in normalized
            and "ORDER BY turn_count DESC" in normalized
        ):
            session_id = str(parameters[0])
            candidates = [
                row
                for (stored_session_id, _), row in (
                    self._database.decisions_by_turn.items()
                )
                if stored_session_id == session_id
            ]
            return _Result(
                None
                if not candidates
                else max(candidates, key=lambda row: int(row["turn_count"]))
            )
        raise AssertionError(f"unexpected SQL: {normalized}")


class _FakePool:
    def __init__(self, database: _FakeM6Database) -> None:
        self.database = database

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        self.database.connection_count += 1
        yield _FakeConnection(self.database)


class _UnavailablePool:
    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        raise PostgresConnectionError("PostgreSQL connection is unavailable")
        yield  # pragma: no cover


def test_initialize_delegates_to_the_shared_migration_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _repository_module()
    database = _FakeM6Database()
    pool = _FakePool(database)
    calls: list[Any] = []
    migration_module = ModuleType(
        "course_insight.infrastructure.postgresql.migration_runner"
    )
    migration_module.run_migrations = calls.append  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        migration_module.__name__,
        migration_module,
    )

    module.PostgresM6Repository(pool).initialize()

    assert calls == [pool]


def test_session_snapshots_append_restore_and_replay_as_jsonb() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    first = _record(seed).result.session_state_snapshot

    assert repository.get_latest_session_state("session_1") is None
    repository.save_session_state(seed)
    repository.save_session_state(first)
    repository.save_session_state(first.model_copy(deep=True))

    restored_seed = repository.get_session_state("session_1", 0)
    restored_first = repository.get_session_state("session_1", 1)
    latest = repository.get_latest_session_state("session_1")
    assert restored_seed == seed
    assert restored_first == first
    assert latest == first
    assert restored_seed is not seed
    assert restored_first is not first
    assert sorted(database.snapshots) == [("session_1", 0), ("session_1", 1)]
    assert database.jsonb_bind_count == 2
    assert database.snapshots[("session_1", 1)]["payload_checksum"] == (
        first.content_checksum()
    )
    assert database.snapshots[("session_1", 1)]["schema_version"] == (
        first.schema_version
    )
    assert any(
        "ORDER BY turn_count DESC" in query
        for query, _ in database.executed
    )


def test_noncontiguous_snapshot_fails_closed_without_changing_history() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    repository.save_session_state(_snapshot())
    invalid = _snapshot(
        state="S4",
        turn_count=2,
        action_ids=["action_1", "action_2"],
    )

    with pytest.raises(DomainError) as raised:
        repository.save_session_state(invalid)

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert sorted(database.snapshots) == [("session_1", 0)]


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda row: row["payload"].update({"session_id": "other"}),
            id="payload-column-identity",
        ),
        pytest.param(
            lambda row: row.update({"payload_checksum": "0" * 64}),
            id="payload-checksum",
        ),
        pytest.param(
            lambda row: row.update({"schema_version": "99.0.0"}),
            id="stored-schema-version",
        ),
        pytest.param(
            lambda row: row["payload"].update({"schema_version": "99.0.0"}),
            id="payload-schema-version",
        ),
    ],
)
def test_snapshot_read_fails_closed_for_corrupt_payload(tamper: Any) -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    repository.save_session_state(_snapshot())
    tamper(database.snapshots[("session_1", 0)])

    with pytest.raises(module.PostgresOperationError, match="integrity"):
        repository.get_session_state("session_1", 0)


def test_commit_atomically_seeds_previous_result_and_decision() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    candidate = _record(seed)

    stored = repository.commit_decision(candidate, seed)

    assert stored == candidate
    assert stored is not candidate
    assert sorted(database.snapshots) == [("session_1", 0), ("session_1", 1)]
    assert len(database.decisions_by_turn) == 1
    assert repository.get_session_state("session_1", 0) == seed
    assert repository.get_session_state("session_1", 1) == (
        candidate.result.session_state_snapshot
    )
    assert repository.get_decision_by_request("a" * 64) == candidate
    assert repository.get_latest_decision("session_1") == candidate
    assert database.transaction_count == 1
    assert database.advisory_lock_count == 1
    decision_row = database.decisions_by_request["a" * 64]
    assert decision_row["payload_checksum"] == candidate.result.content_checksum()
    assert decision_row["schema_version"] == candidate.result.schema_version
    assert database.jsonb_bind_count == 4


def test_request_and_input_replays_return_only_compatible_authority() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    first = _record(seed)
    assert repository.commit_decision(first, seed) == first

    input_replay = replace(first, request_fingerprint="e" * 64)
    assert repository.commit_decision(input_replay, seed) == first

    request_collision = _record(
        seed,
        request_fingerprint=first.request_fingerprint,
        input_fingerprint="f" * 64,
    )
    with pytest.raises(DomainError) as request_error:
        repository.commit_decision(request_collision, seed)
    assert request_error.value.code == "TUTORING_REFERENCE_MISMATCH"

    changed_result = first.result.model_copy(
        update={"created_at": first.result.created_at + timedelta(seconds=1)},
        deep=True,
    )
    divergent_input_replay = replace(
        first,
        request_fingerprint="9" * 64,
        result=changed_result,
    )
    with pytest.raises(DomainError) as input_error:
        repository.commit_decision(divergent_input_replay, seed)
    assert input_error.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert len(database.decisions_by_turn) == 1


def test_competing_input_from_same_cursor_is_rejected_as_stale() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    first = _record(seed)
    repository.commit_decision(first, seed)
    competitor = _record(
        seed,
        request_fingerprint="e" * 64,
        input_fingerprint="f" * 64,
    )

    with pytest.raises(DomainError) as raised:
        repository.commit_decision(competitor, seed)

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert sorted(database.snapshots) == [("session_1", 0), ("session_1", 1)]
    assert len(database.decisions_by_turn) == 1


def test_decision_insert_failure_rolls_back_both_snapshots() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    database.fail_decision_insert = True
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()

    with pytest.raises(RuntimeError, match="forced decision insert failure"):
        repository.commit_decision(_record(seed), seed)

    assert database.snapshots == {}
    assert database.decisions_by_turn == {}


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda row: row.update({"payload_checksum": "0" * 64}),
            id="result-checksum",
        ),
        pytest.param(
            lambda row: row.update({"schema_version": "99.0.0"}),
            id="stored-schema-version",
        ),
        pytest.param(
            lambda row: row["result_payload"].update(
                {"schema_version": "99.0.0"}
            ),
            id="payload-schema-version",
        ),
        pytest.param(
            lambda row: row.update({"evidence_fingerprint": "0" * 64}),
            id="evidence-fingerprint",
        ),
    ],
)
def test_decision_read_fails_closed_for_corrupt_payload(tamper: Any) -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    candidate = _record(seed)
    repository.commit_decision(candidate, seed)
    tamper(database.decisions_by_request[candidate.request_fingerprint])

    with pytest.raises(module.PostgresOperationError, match="integrity"):
        repository.get_decision_by_request(candidate.request_fingerprint)


def test_twenty_concurrent_commits_return_one_authoritative_decision() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    candidate = _record(seed)

    def commit(_: int) -> TutoringDecisionRecord:
        return repository.commit_decision(
            candidate.isolated_copy(),
            seed.model_copy(deep=True),
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        decisions = list(executor.map(commit, range(20)))

    assert len({item.result.content_checksum() for item in decisions}) == 1
    assert len(database.decisions_by_turn) == 1
    assert sorted(database.snapshots) == [("session_1", 0), ("session_1", 1)]


def test_stable_connection_error_is_not_rewritten_or_chained() -> None:
    module = _repository_module()
    repository = module.PostgresM6Repository(_UnavailablePool())

    with pytest.raises(PostgresConnectionError) as raised:
        repository.get_latest_session_state("session_1")

    assert str(raised.value) == "PostgreSQL connection is unavailable"
    assert raised.value.__cause__ is None


def test_policy_execution_uses_jsonb_and_request_first_writer() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    first = _policy_execution(policy_id="policy_first")
    competitor = _policy_execution(policy_id="policy_competitor")

    assert repository.commit_policy_execution(first) == first
    assert repository.commit_policy_execution(competitor) == first
    assert repository.get_policy_execution_by_request(
        first.request_fingerprint
    ) == first
    assert database.jsonb_bind_count == 1


def test_decision_and_policy_observation_share_one_transaction() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    seed = _snapshot()
    execution = _policy_execution()
    observation = PolicyObservation(
        policy_execution_fingerprint=execution.policy_execution_fingerprint,
        request_fingerprint=execution.request_fingerprint,
        feature_schema_version=execution.feature_schema_version,
        candidate_ids=("candidate_a",),
        selected_candidate_id="candidate_a",
        propensity=1.0,
    )
    candidate = replace(
        _record(seed),
        policy_execution_ref=execution,
        policy_observation=observation,
    )
    repository.commit_policy_execution(execution)
    before_transactions = database.transaction_count

    stored = repository.commit_decision(candidate, seed)

    assert stored == candidate
    assert database.transaction_count == before_transactions + 1
    assert database.policy_observations[candidate.decision_id][
        "payload_checksum"
    ] == observation.identity


def test_artifact_reward_and_evaluation_round_trip_with_checksums() -> None:
    module = _repository_module()
    database = _FakeM6Database()
    repository = module.PostgresM6Repository(_FakePool(database))
    artifact = PolicyArtifactManifest(
        policy_id="policy_linucb_v1",
        adapter_id="linucb",
        adapter_version="1",
        algorithm="linucb",
        state_graph_version="m6-state-graph-v1",
        baseline_policy_version="m6-rules-v1",
        artifact_sha256="c" * 64,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-actions-v1",
        reward_version="m6-reward-v1",
        gate_policy_version="m6-gate-v1",
        training_data_watermark="2026-07-27T00:00:00Z",
        training_data_checksum="d" * 64,
        status="approved",
        created_at="2026-07-27T01:00:00Z",
        artifact_reference="policies/linucb-v1.json",
        allowed_scopes=("course:school-a", "class:class-a"),
    )
    execution = _policy_execution()
    reward = PolicyRewardRecord(
        policy_execution_fingerprint=execution.policy_execution_fingerprint,
        outcome_identity="outcome_1",
        status="observed",
        reward=0.8,
    )
    evaluation = PolicyEvaluationRecord(
        policy_id=artifact.policy_id,
        dataset_identity="dataset_1",
        status="sufficient_data",
        approved=True,
        effective_sample_size=25.0,
        action_coverage=0.75,
        observation_count=30,
    )
    repository.commit_policy_execution(execution)

    assert repository.save_policy_artifact(artifact) == artifact
    assert repository.save_policy_reward(reward) == reward
    assert repository.save_policy_evaluation(evaluation) == evaluation
    assert repository.get_policy_artifact(artifact.policy_id) == artifact
    assert repository.get_policy_reward(
        execution.policy_execution_fingerprint
    ) == reward
    assert repository.get_policy_evaluation(
        evaluation.policy_id,
        evaluation.dataset_identity,
    ) == evaluation

    database.policy_rewards[
        (execution.policy_execution_fingerprint, reward.reward_version)
    ]["payload_checksum"] = "0" * 64
    with pytest.raises(module.PostgresOperationError):
        repository.get_policy_reward(execution.policy_execution_fingerprint)
