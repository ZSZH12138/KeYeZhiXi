from __future__ import annotations

import importlib
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.contracts.assessment import (
    CriterionScore,
    RemediationPlan,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite import (
    SCHEMA_VERSION,
    connect_sqlite,
    current_schema_version,
    migrate,
)
from course_insight.modules.m6_tutoring_fsm.service import (
    M6TutoringControlService,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)


NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def _repository_type() -> type[Any]:
    try:
        module = importlib.import_module(
            "course_insight.infrastructure.sqlite.m6_repository"
        )
    except ImportError as error:
        pytest.fail(f"SQLiteM6Repository is unavailable: {error}")
    repository_type = getattr(module, "SQLiteM6Repository", None)
    if repository_type is None:
        pytest.fail("SQLiteM6Repository is unavailable from the M6 SQLite module")
    return repository_type


def _repository(database_path: Path) -> Any:
    repository = _repository_type()(database_path)
    repository.initialize()
    return repository


def _service(repository: Any) -> M6TutoringControlService:
    return M6TutoringControlService(DEFAULT_STATE_MACHINE, repository)


def _task_plan() -> TaskPlan:
    return TaskPlan(
        task_id="task_1",
        task_type="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        blueprint_id="blueprint_1",
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )


def _criterion_score(*, score: float) -> CriterionScore:
    return CriterionScore(
        criterion_id="criterion_1",
        score=score,
        student_evidence="supported correction" if score else "",
        course_evidence_id="evidence_1" if score else None,
        reason="Deterministic rule-scoring result.",
    )


def _audit_record(version: int) -> ScoreAuditRecord:
    score = 0.0 if version == 1 else 1.0
    return ScoreAuditRecord(
        audit_id="audit_1",
        audit_version=version,
        attempt_id="attempt_1",
        item_instance_id="item_instance_1",
        criterion_scores=[_criterion_score(score=score)],
        total_score=score,
        max_score=1.0,
        confidence=1.0,
        scoring_method="rule" if version == 1 else "teacher_override",
        review_status="completed",
        review_reason=[],
        created_at=NOW + timedelta(minutes=version - 1),
    )


def _scoring_result(version: int = 1) -> ScoringResultBundle:
    records = [_audit_record(audit_version) for audit_version in range(1, version + 1)]
    current_score = records[-1].total_score
    return ScoringResultBundle(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="pseudonym_learner",
        score_audit_records=records,
        learning_events=[],
        remediation_plan=RemediationPlan(
            plan_id=f"remediation_{version}",
            based_on_attempt_id="attempt_1",
            learner_id="pseudonym_learner",
            targets=[],
            created_at=NOW + timedelta(minutes=version - 1),
        ),
        total_score=current_score,
        max_score=1.0,
        finalized_at=NOW + timedelta(minutes=version - 1),
    )


def _state_update(version: int = 1) -> StateUpdateResult:
    updated_at = NOW + timedelta(minutes=version - 1)
    audit_identity = f"audit_1:{version}"
    diagnosis = DiagnosisResult(
        diagnosis_id=f"diagnosis_{version}",
        attempt_id="attempt_1",
        learner_id="pseudonym_learner",
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="item_instance_1",
                concept_ids=["concept_1"],
                misconception_ids=[],
                error_type="none",
                confidence=1.0,
                evidence_audit_ids=[audit_identity],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=["concept_1"],
        priority_misconception_ids=[],
        generated_at=updated_at,
    )
    learner = LearnerStateSnapshot(
        snapshot_id=f"learner_snapshot_{version}",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        state_version=version,
        concept_states=[
            ConceptState(
                concept_id="concept_1",
                mastery_probability=0.5 if version == 1 else 1.0,
                mastery_confidence=1.0,
                misconceptions=[],
                hint_dependency=0.0,
                recent_correction_rate=1.0,
                evidence_count=version,
                updated_at=updated_at,
            )
        ],
        overall_mastery=0.5 if version == 1 else 1.0,
        evidence_count=version,
        updated_at=updated_at,
    )
    class_state = ClassStateSnapshot(
        snapshot_id=f"class_snapshot_{version}",
        course_id="course_1",
        class_id="class_1",
        aggregation_policy_version="1.0.0",
        scope={"course_id": "course_1", "class_id": "class_1"},
        class_size=1,
        assessed_count=1,
        coverage_rate=1.0,
        concept_status=[],
        misconception_summary=[],
        evidence_status="sufficient",
        updated_at=updated_at,
    )
    return StateUpdateResult(
        diagnosis_result=diagnosis,
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=[
            f"audit_1:{audit_version}"
            for audit_version in range(1, version + 1)
        ],
        updated_at=updated_at,
    )


def _inputs(
    version: int = 1,
) -> tuple[TaskPlan, ScoringResultBundle, StateUpdateResult]:
    return _task_plan(), _scoring_result(version), _state_update(version)


def _snapshot(turn_count: int) -> SessionStateSnapshot:
    states = {0: "S1", 1: "S3", 2: "S4"}
    return SessionStateSnapshot(
        session_id="session_1",
        current_state=states[turn_count],
        turn_count=turn_count,
        completed_action_ids=[
            f"action_{turn}" for turn in range(1, turn_count + 1)
        ],
        updated_at=NOW + timedelta(minutes=turn_count),
    )


def _database_counts(database_path: Path) -> tuple[int, int]:
    with connect_sqlite(database_path) as connection:
        snapshots = int(
            connection.execute("SELECT COUNT(*) FROM m6_session_states").fetchone()[
                0
            ]
        )
        decisions = int(
            connection.execute(
                "SELECT COUNT(*) FROM m6_tutoring_decisions"
            ).fetchone()[0]
        )
    return snapshots, decisions


def _stored_turns(database_path: Path) -> list[int]:
    with connect_sqlite(database_path) as connection:
        return [
            int(row[0])
            for row in connection.execute(
                """
                SELECT turn_count
                FROM m6_session_states
                WHERE session_id = ?
                ORDER BY turn_count
                """,
                ("session_1",),
            ).fetchall()
        ]


def _only_request_fingerprint(database_path: Path) -> str:
    with connect_sqlite(database_path) as connection:
        rows = connection.execute(
            "SELECT request_fingerprint FROM m6_tutoring_decisions"
        ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0])


def test_migration_v3_creates_m6_decision_table_with_required_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        migrate(connection)

        assert SCHEMA_VERSION == 3
        assert current_schema_version(connection) == 3
        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3]

        columns = {
            str(row["name"]): row
            for row in connection.execute(
                "PRAGMA table_info('m6_tutoring_decisions')"
            ).fetchall()
        }
        assert list(columns) == [
            "decision_id",
            "session_id",
            "turn_count",
            "previous_turn_count",
            "request_fingerprint",
            "input_fingerprint",
            "evidence_fingerprint",
            "evidence_identity",
            "result_payload",
        ]
        assert int(columns["decision_id"]["pk"]) == 1
        assert int(columns["previous_turn_count"]["notnull"]) == 0
        assert all(
            int(column["notnull"]) == 1 or int(column["pk"]) == 1
            for name, column in columns.items()
            if name != "previous_turn_count"
        )

        foreign_keys = {
            (str(row["from"]), str(row["to"]))
            for row in connection.execute(
                "PRAGMA foreign_key_list('m6_tutoring_decisions')"
            ).fetchall()
        }
        assert {
            ("session_id", "session_id"),
            ("turn_count", "turn_count"),
        } <= foreign_keys

        unique_columns: set[frozenset[str]] = set()
        for index in connection.execute(
            "PRAGMA index_list('m6_tutoring_decisions')"
        ).fetchall():
            if int(index["unique"]) != 1:
                continue
            index_name = str(index["name"]).replace("'", "''")
            indexed = connection.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
            unique_columns.add(frozenset(str(row["name"]) for row in indexed))
        assert {
            frozenset({"request_fingerprint"}),
            frozenset({"input_fingerprint"}),
            frozenset({"session_id", "turn_count"}),
        } <= unique_columns


@pytest.mark.parametrize("invalid_column", ["evidence_identity", "result_payload"])
def test_m6_decision_json_columns_require_canonical_json(
    tmp_path: Path,
    invalid_column: str,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute(
            """
            INSERT INTO m6_session_states(session_id, turn_count, payload)
            VALUES (?, ?, ?)
            """,
            ("session_1", 0, dumps_json(_snapshot(0).to_dict())),
        )
        evidence_identity = '{"audit":"audit_1:1"}'
        result_payload = "{}"
        if invalid_column == "evidence_identity":
            evidence_identity = '{ "audit": "audit_1:1" }'
        else:
            result_payload = "{ }"

        with pytest.raises(sqlite3.IntegrityError):
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
                    f"decision_invalid_{invalid_column}",
                    "session_1",
                    0,
                    None,
                    f"request_{invalid_column}",
                    f"input_{invalid_column}",
                    f"evidence_{invalid_column}",
                    evidence_identity,
                    result_payload,
                ),
            )


def test_m6_decision_requires_its_persisted_session_turn(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    with connect_sqlite(database_path) as connection:
        migrate(connection)

        with pytest.raises(sqlite3.IntegrityError):
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
                    "decision_without_snapshot",
                    "session_missing",
                    1,
                    0,
                    "request_missing",
                    "input_missing",
                    "evidence_missing",
                    "{}",
                    "{}",
                ),
            )


def test_sqlite_repository_saves_exact_snapshots_and_retains_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository(database_path)
    snapshots = [_snapshot(turn) for turn in range(3)]

    assert repository.get_latest_session_state("session_1") is None
    for snapshot in snapshots:
        repository.save_session_state(snapshot)

    restored = [
        repository.get_session_state("session_1", turn) for turn in range(3)
    ]
    latest = repository.get_latest_session_state("session_1")

    assert restored == snapshots
    assert all(item is not original for item, original in zip(restored, snapshots))
    assert latest == snapshots[-1]
    assert latest is not snapshots[-1]
    assert _stored_turns(database_path) == [0, 1, 2]


def test_first_decision_persists_seed_result_and_readable_decision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository(database_path)
    service = _service(repository)

    result = service.decide_next_action(*_inputs(), None)

    seed = repository.get_session_state("session_1", 0)
    assert seed == _snapshot(0).model_copy(update={"updated_at": NOW}, deep=True)
    assert repository.get_session_state("session_1", 1) == (
        result.session_state_snapshot
    )
    assert repository.get_latest_session_state("session_1") == (
        result.session_state_snapshot
    )
    assert result.session_state_snapshot.turn_count == 1
    assert _database_counts(database_path) == (2, 1)
    assert _stored_turns(database_path) == [0, 1]

    request_fingerprint = _only_request_fingerprint(database_path)
    decision = repository.get_decision_by_request(request_fingerprint)
    assert decision is not None
    assert decision == repository.get_latest_decision("session_1")
    assert decision.session_id == "session_1"
    assert decision.turn_count == 1
    assert decision.previous_turn_count == 0
    assert decision.request_fingerprint == request_fingerprint
    assert len(decision.input_fingerprint) == 64
    assert len(decision.evidence_fingerprint) == 64
    assert decision.result == result
    assert decision.result is not result


def test_same_request_replay_returns_original_result_without_advancing(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository(database_path)
    service = _service(repository)

    first = service.decide_next_action(*_inputs(), None)
    replay = service.decide_next_action(*_inputs(), None)

    assert replay == first
    assert replay.content_checksum() == first.content_checksum()
    assert replay.teaching_action.action_id == first.teaching_action.action_id
    assert replay.evidence_query.query_id == first.evidence_query.query_id
    assert replay.feedback_generation_task.feedback_task_id == (
        first.feedback_generation_task.feedback_task_id
    )
    assert replay.session_state_snapshot.turn_count == 1
    assert _database_counts(database_path) == (2, 1)
    assert _stored_turns(database_path) == [0, 1]


def test_replay_that_becomes_visible_during_stale_check_is_returned(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository(database_path)
    previous = _snapshot(0)
    first = _service(repository).decide_next_action(
        *_inputs(),
        previous.model_copy(deep=True),
    )

    class DelayedReplayRepository:
        def __init__(self, delegate: Any) -> None:
            self._delegate = delegate
            self.request_lookups = 0

        def get_decision_by_request(self, request_key: str) -> Any:
            self.request_lookups += 1
            if self.request_lookups == 1:
                return None
            return self._delegate.get_decision_by_request(request_key)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._delegate, name)

    delayed_repository = DelayedReplayRepository(repository)
    replay = _service(delayed_repository).decide_next_action(
        *_inputs(),
        previous.model_copy(deep=True),
    )

    assert replay == first
    assert delayed_repository.request_lookups == 2
    assert _database_counts(database_path) == (2, 1)


def test_new_service_instance_recovers_latest_turn_and_preserves_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    first_repository = _repository(database_path)
    first = _service(first_repository).decide_next_action(*_inputs(1), None)

    restarted_repository = _repository(database_path)
    second = _service(restarted_repository).decide_next_action(*_inputs(2), None)

    assert second.session_state_snapshot.turn_count == 2
    assert second.session_state_snapshot.completed_action_ids[:-1] == (
        first.session_state_snapshot.completed_action_ids
    )
    assert restarted_repository.get_session_state("session_1", 1) == (
        first.session_state_snapshot
    )
    assert restarted_repository.get_latest_session_state("session_1") == (
        second.session_state_snapshot
    )
    latest_decision = restarted_repository.get_latest_decision("session_1")
    assert latest_decision is not None
    assert latest_decision.result == second
    assert _database_counts(database_path) == (3, 2)
    assert _stored_turns(database_path) == [0, 1, 2]


@pytest.mark.parametrize("caller_snapshot_kind", ["stale", "conflicting"])
def test_stale_or_conflicting_caller_snapshot_cannot_overwrite_history(
    tmp_path: Path,
    caller_snapshot_kind: str,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository(database_path)
    service = _service(repository)
    first = service.decide_next_action(*_inputs(1), None)
    if caller_snapshot_kind == "stale":
        caller_snapshot = repository.get_session_state("session_1", 0)
    else:
        caller_snapshot = first.session_state_snapshot.model_copy(
            update={"updated_at": NOW + timedelta(seconds=30)},
            deep=True,
        )
    assert caller_snapshot is not None

    with pytest.raises(DomainError) as captured:
        service.decide_next_action(*_inputs(2), caller_snapshot)

    assert captured.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert repository.get_latest_session_state("session_1") == (
        first.session_state_snapshot
    )
    assert repository.get_latest_decision("session_1").result == first
    assert _database_counts(database_path) == (2, 1)
    assert _stored_turns(database_path) == [0, 1]


def test_decision_insert_failure_rolls_back_seed_snapshot_and_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository(database_path)
    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_m6_decision_insert
            BEFORE INSERT ON m6_tutoring_decisions
            BEGIN
                SELECT RAISE(ABORT, 'forced M6 decision failure');
            END
            """
        )

    with pytest.raises((sqlite3.DatabaseError, RuntimeError, DomainError)):
        _service(repository).decide_next_action(*_inputs(), None)

    assert repository.get_latest_session_state("session_1") is None
    assert repository.get_latest_decision("session_1") is None
    assert _database_counts(database_path) == (0, 0)


def test_twenty_concurrent_same_requests_create_one_authoritative_decision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    _repository(database_path)

    def decide_once(_: int) -> TutoringControlResult:
        repository = _repository_type()(database_path)
        return _service(repository).decide_next_action(*_inputs(), None)

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(decide_once, range(20)))

    assert len({result.content_checksum() for result in results}) == 1
    assert len({result.teaching_action.action_id for result in results}) == 1
    assert len({result.evidence_query.query_id for result in results}) == 1
    assert len(
        {
            result.feedback_generation_task.feedback_task_id
            for result in results
        }
    ) == 1
    assert {result.session_state_snapshot.turn_count for result in results} == {1}
    assert {
        tuple(result.session_state_snapshot.completed_action_ids)
        for result in results
    } == {(results[0].teaching_action.action_id,)}
    assert _database_counts(database_path) == (2, 1)
    assert _stored_turns(database_path) == [0, 1]

    repository = _repository_type()(database_path)
    assert repository.get_latest_session_state("session_1") == (
        results[0].session_state_snapshot
    )
    assert repository.get_latest_decision("session_1").result == results[0]


def test_twenty_competing_requests_from_one_cursor_allow_only_one_input(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    _repository(database_path)
    previous = _snapshot(0)

    def decide_once(index: int) -> tuple[str, int, TutoringControlResult | DomainError]:
        version = 1 if index % 2 == 0 else 2
        repository = _repository_type()(database_path)
        try:
            result = _service(repository).decide_next_action(
                *_inputs(version),
                previous.model_copy(deep=True),
            )
            return "accepted", version, result
        except DomainError as error:
            return "rejected", version, error

    with ThreadPoolExecutor(max_workers=20) as executor:
        outcomes = list(executor.map(decide_once, range(20)))

    accepted = [outcome for outcome in outcomes if outcome[0] == "accepted"]
    rejected = [outcome for outcome in outcomes if outcome[0] == "rejected"]
    outcome_summary = Counter(
        (
            status,
            version,
            result.code if isinstance(result, DomainError) else "result",
        )
        for status, version, result in outcomes
    )
    assert len(accepted) == 10, outcome_summary
    assert len(rejected) == 10, outcome_summary
    assert len({outcome[1] for outcome in accepted}) == 1
    assert len({outcome[1] for outcome in rejected}) == 1
    assert accepted[0][1] != rejected[0][1]
    assert all(
        isinstance(outcome[2], DomainError)
        and outcome[2].code == "TUTORING_REFERENCE_MISMATCH"
        for outcome in rejected
    )
    accepted_results = [
        outcome[2]
        for outcome in accepted
        if isinstance(outcome[2], TutoringControlResult)
    ]
    assert len({result.content_checksum() for result in accepted_results}) == 1
    assert _database_counts(database_path) == (2, 1)
    assert _stored_turns(database_path) == [0, 1]
