from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Lock, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from course_insight.application.coordinator import AppCoordinator
from course_insight.application.assessment_recovery import AssessmentRecovery
from course_insight.contracts.analytics import TeacherReviewDecision
from course_insight.contracts.platform import (
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import LearningObservationBatch
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
)
from course_insight.infrastructure.sqlite.outbox_migration import (
    LEGACY_OUTBOX_SQL,
)
from course_insight.infrastructure.sqlite.workflow_migration import (
    ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL,
    ASSESSMENT_RUNS_SUBMIT_INDEX_SQL,
    ASSESSMENT_RUNS_V5_SQL,
)
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m0_platform.workflow import (
    AssessmentRun,
    advance_run,
)
from tests.integration.test_m6_cross_module import (
    _decide,
    _complete_inputs,
    _knowledge_bundle,
    _ready_index_ref,
    _RecordingM6,
    _scoring_result,
    _sqlite_m6_repository,
    _state_update,
    _task_plan,
)
from course_insight.modules.m6_tutoring_fsm.service import (
    M6TutoringControlService,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyExecutionRef,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)
from tests.integration.test_web_workflow_persistence import (
    _analytics,
    _feedback,
    _paper,
    _review,
)


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


def _run(
    *,
    operation_id: str = "request_1",
    operation: str = "submit",
    request_checksum: str = "checksum_1",
    attempt_id: str = "attempt_1",
    created_at: datetime = NOW,
    checkpoint: str = "pending",
    status: str = "pending",
    version: int = 1,
    locked_by: str | None = None,
    lease_until: datetime | None = None,
    scoring_result_checksum: str | None = "a" * 64,
    state_version: int | None = 1,
    previous_state_frozen: bool | None = False,
) -> AssessmentRun:
    return AssessmentRun(
        operation_id=operation_id,
        operation=operation,
        request_checksum=request_checksum,
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        task_id="task_1",
        paper_id="paper_1",
        attempt_id=attempt_id,
        feedback_id="feedback_1",
        report_id="report_1",
        checkpoint=checkpoint,
        status=status,
        version=version,
        locked_by=locked_by,
        lease_until=lease_until,
        error_code=None,
        created_at=created_at,
        updated_at=created_at,
        scoring_result_checksum=scoring_result_checksum,
        target_audit_id=("audit_1" if operation == "review" else None),
        target_audit_version=(1 if operation == "review" else None),
        state_version=state_version,
        previous_state_frozen=previous_state_frozen,
    )


def _repository(database_path: Path) -> SQLiteM0Repository:
    repository = SQLiteM0Repository(database_path)
    repository.initialize()
    return repository


def test_submit_state_machine_rejects_checkpoint_jumps() -> None:
    run = _run()

    with pytest.raises(DomainError) as captured:
        advance_run(run, "state_saved", now=NOW)

    assert captured.value.code == "WORKFLOW_TRANSITION_INVALID"
    assert run.checkpoint == "pending"
    assert run.version == 1


def test_repository_claim_is_atomic_and_returns_isolated_records(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(database_path)
    repository.insert_or_get_assessment_run(_run())
    barrier = Barrier(2)
    winners: list[str] = []
    winner_lock = Lock()

    def claim(worker_id: str) -> None:
        contender = SQLiteM0Repository(database_path)
        barrier.wait()
        claimed = contender.claim_assessment_run(
            "request_1",
            worker_id=worker_id,
            now=NOW,
            lease_until=NOW + timedelta(seconds=30),
        )
        if claimed is not None:
            with winner_lock:
                winners.append(worker_id)

    threads = [
        Thread(target=claim, args=("worker_a",)),
        Thread(target=claim, args=("worker_b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    stored = repository.get_assessment_run("request_1")
    assert stored is not None
    assert stored.locked_by == winners[0]
    assert stored.checkpoint == "claimed"
    assert stored is not repository.get_assessment_run("request_1")


def test_repository_reclaims_expired_lease_from_last_checkpoint(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "workflow.db")
    repository.insert_or_get_assessment_run(_run())
    claimed = repository.claim_assessment_run(
        "request_1",
        worker_id="dead_worker",
        now=NOW,
        lease_until=NOW + timedelta(seconds=5),
    )
    assert claimed is not None
    saved = repository.advance_assessment_run(
        "request_1",
        expected_version=claimed.version,
        checkpoint="scoring_saved",
        worker_id="dead_worker",
        now=NOW + timedelta(seconds=1),
    )

    assert repository.claim_assessment_run(
        "request_1",
        worker_id="early_worker",
        now=NOW + timedelta(seconds=4),
        lease_until=NOW + timedelta(seconds=30),
    ) is None
    reclaimed = repository.reclaim_assessment_run(
        "request_1",
        worker_id="new_worker",
        now=NOW + timedelta(seconds=6),
        lease_until=NOW + timedelta(seconds=36),
    )

    assert reclaimed.checkpoint == saved.checkpoint
    assert reclaimed.locked_by == "new_worker"
    assert reclaimed.version == saved.version + 1


def test_repository_replay_and_conflict_do_not_store_submission_payload(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(database_path)
    candidate = _run(request_checksum="sha256_only")

    first = repository.insert_or_get_assessment_run(candidate)
    replay = repository.insert_or_get_assessment_run(
        _run(
            request_checksum="sha256_only",
            created_at=NOW + timedelta(minutes=1),
        ),
    )

    assert replay == first
    with pytest.raises(DomainError) as operation_conflict:
        repository.insert_or_get_assessment_run(
            _run(operation_id="request_2", request_checksum="different"),
        )
    assert operation_conflict.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"

    raw_database = database_path.read_bytes()
    assert b"answers" not in raw_database
    assert b"submission" not in raw_database
    assert b"sha256_only" in raw_database


def test_repository_failure_and_completion_preserve_checkpoint_and_clear_lease(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "workflow.db")
    repository.insert_or_get_assessment_run(_run())
    claimed = repository.claim_assessment_run(
        "request_1",
        worker_id="worker_1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
    )
    assert claimed is not None
    scoring = repository.advance_assessment_run(
        "request_1",
        expected_version=claimed.version,
        checkpoint="scoring_saved",
        worker_id="worker_1",
        now=NOW + timedelta(seconds=1),
    )
    failed = repository.fail_assessment_run(
        "request_1",
        expected_version=scoring.version,
        worker_id="worker_1",
        error_code="SCORING_TEMPORARILY_UNAVAILABLE",
        now=NOW + timedelta(seconds=2),
    )

    assert failed.checkpoint == "scoring_saved"
    assert failed.status == "failed"
    assert failed.locked_by is None
    resumed = repository.claim_assessment_run(
        "request_1",
        worker_id="worker_2",
        now=NOW + timedelta(seconds=3),
        lease_until=NOW + timedelta(seconds=33),
    )
    assert resumed is not None
    assert resumed.checkpoint == "scoring_saved"
    current = resumed
    for checkpoint in (
        "events_appended",
        "state_inputs_frozen",
        "state_saved",
        "policy_frozen",
        "tutoring_saved",
        "feedback_saved",
        "analytics_saved",
    ):
        baseline = (
            {"previous_state_frozen": True}
            if checkpoint == "state_inputs_frozen"
            else (
                {
                    "policy_id": "m6-deterministic-v1",
                    "adapter_id": "m6-rules-adapter",
                    "adapter_version": "v1",
                    "artifact_sha256": None,
                    "feature_schema_version": "m6-features-v1",
                    "action_space_version": "m6-action-space-v1",
                    "gate_policy_version": "m6-active-gate-v1",
                }
                if checkpoint == "policy_frozen"
                else {}
            )
        )
        current = repository.advance_assessment_run(
            "request_1",
            expected_version=current.version,
            checkpoint=checkpoint,
            worker_id="worker_2",
            now=current.updated_at + timedelta(seconds=1),
            **baseline,
        )
    completed = repository.complete_assessment_run(
        "request_1",
        expected_version=current.version,
        worker_id="worker_2",
        now=current.updated_at + timedelta(seconds=1),
    )

    assert completed.status == "completed"
    assert completed.locked_by is None
    assert completed.lease_until is None


def test_repository_allows_next_review_version_after_prior_review_completes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "workflow.db")
    first = _run(
        operation_id="review_1",
        operation="review",
        request_checksum="audit_1:v1",
        checkpoint="completed",
        status="completed",
        version=2,
    )
    second = _run(
        operation_id="review_2",
        operation="review",
        request_checksum="audit_1:v2",
    )

    repository.insert_or_get_assessment_run(first)
    repository.insert_or_get_assessment_run(second)

    assert (
        repository.get_assessment_run_by_paper(
            "paper_1",
            operation="review",
        ).operation_id
        == "review_2"
    )
    assert (
        repository.get_assessment_run_by_attempt(
            "attempt_1",
            operation="review",
        ).operation_id
        == "review_2"
    )


def test_repository_allows_only_one_nonterminal_review_per_paper_concurrently(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    _repository(database_path)
    barrier = Barrier(2)
    winners: list[str] = []
    error_codes: list[str] = []
    winner_lock = Lock()
    candidates = (
        _run(
            operation_id="review_1",
            operation="review",
            request_checksum="audit_1:v1",
        ),
        _run(
            operation_id="review_2",
            operation="review",
            request_checksum="audit_1:v2",
        ),
    )

    def create_and_claim(run: AssessmentRun, worker_id: str) -> None:
        contender = SQLiteM0Repository(database_path)
        barrier.wait()
        try:
            recorded = contender.insert_or_get_assessment_run(run)
            claimed = contender.claim_assessment_run(
                recorded.operation_id,
                worker_id=worker_id,
                now=NOW,
                lease_until=NOW + timedelta(seconds=30),
            )
            assert claimed is not None
            with winner_lock:
                winners.append(recorded.operation_id)
        except DomainError as error:
            with winner_lock:
                error_codes.append(error.code)

    threads = [
        Thread(target=create_and_claim, args=(candidates[0], "worker_a")),
        Thread(target=create_and_claim, args=(candidates[1], "worker_b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert error_codes == ["WORKFLOW_BUSY"]
    stored = _repository(database_path).get_assessment_run_by_paper(
        "paper_1",
        operation="review",
    )
    assert stored is not None
    assert stored.operation_id == winners[0]


def test_explicit_v8_migration_preserves_legacy_v5_workflow_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute("DROP INDEX m0_one_submit_per_paper")
        connection.execute("DROP TABLE m0_assessment_runs")
        connection.execute(ASSESSMENT_RUNS_V5_SQL)
        connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
        connection.execute("DROP TABLE m0_event_outbox")
        connection.execute(LEGACY_OUTBOX_SQL)
        connection.execute("DELETE FROM schema_migrations WHERE version >= 6")
        connection.execute(
            """
            INSERT INTO m0_assessment_runs(
                operation_id, operation, request_checksum,
                course_id, class_id, learner_id, session_id,
                task_id, paper_id, attempt_id, feedback_id, report_id,
                checkpoint, status, version, locked_by, lease_until,
                error_code, created_at, updated_at
            ) VALUES (
                'legacy_submit', 'submit', 'legacy_checksum',
                'course_1', 'class_1', 'learner_1', 'session_1',
                'task_1', 'paper_1', 'attempt_1', NULL, NULL,
                'scoring_saved', 'failed', 3, NULL, NULL,
                'SAFE_ERROR', '2026-07-25T08:00:00+00:00',
                '2026-07-25T08:01:00+00:00'
            )
            """
        )
        migrate(connection)

        row = connection.execute(
            """
            SELECT operation_id, checkpoint, version,
                   scoring_result_checksum, target_audit_id,
                   target_audit_version, state_version
            FROM m0_assessment_runs
            WHERE operation_id = 'legacy_submit'
            """
        ).fetchone()
        review_index = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'index'
              AND name = 'm0_one_nonterminal_review_per_paper'
            """
        ).fetchone()
        assert current_schema_version(connection) == SCHEMA_VERSION
        assert tuple(row) == (
            "legacy_submit",
            "scoring_saved",
            3,
            None,
            None,
            None,
            None,
        )
        assert review_index is not None
        assert str(review_index[0]).strip() == (
            ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL.strip()
        )
class _WorkflowStore:
    def __init__(self) -> None:
        self.plan = _task_plan().model_copy(
            update={"learner_id": "learner_1"}
        )
        self.paper = _paper()
        self.scoring = None
        self.scoring_history: list[Any] = []
        self.preparation = _preparation(["rubric_task_1"])
        self.state = None
        self.state_history: list[Any] = []
        self.tutoring = None
        self.feedback = None
        self.analytics = None
        self.analytics_history: dict[str, Any] = {}
        self.review = None
        self.reviews: dict[str, Any] = {}
        self.reviewed = None
        self.fail_after: str | None = None
        self.failed_once = False
        self.scoring_calls = 0
        self.retrieved_queries: list[tuple[str | None, str | None]] = []
        self.scored_task_ids: list[str] = []
        self.policy_execution = _policy_execution()
        self.policy_events: list[str] = []
        self.prepare_results: list[PolicyExecutionRef] = []
        self.tutoring_calls = 0
        self.observation_batches_built: list[LearningObservationBatch] = []
        self.observation_batches_received: list[LearningObservationBatch] = []

    def trip(self, point: str) -> None:
        if self.fail_after == point and not self.failed_once:
            self.failed_once = True
            raise RuntimeError(f"injected failure after {point}")


class _M4:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def create_task_plan(self, **_: Any):
        return self.store.plan.model_copy(deep=True)

    def get_task_plan(self, task_id: str):
        return self.store.plan.model_copy(deep=True) if task_id == "task_1" else None


class _M8:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def generate_paper(self, **_: Any):
        return self.store.paper.model_copy(deep=True)

    def get_paper(self, paper_id: str):
        return self.store.paper.model_copy(deep=True) if paper_id == "paper_1" else None

    def get_scoring_result(self, attempt_id: str):
        value = self.store.reviewed or self.store.scoring
        return None if value is None else value.model_copy(deep=True)

    def get_scoring_result_by_checksum(self, attempt_id: str, checksum: str):
        for value in self.store.scoring_history:
            if value.content_checksum() == checksum:
                return value.model_copy(deep=True)
        return None

    def get_scoring_result_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ):
        for value in self.store.scoring_history:
            if value.get_audit_record(audit_id).audit_version == audit_version:
                return value.model_copy(deep=True)
        return None

    def prepare_scoring(self, **_: Any):
        return self.store.preparation

    def build_observation_batch(self, paper_id: str, bundle):
        latest_version = max(
            record.audit_version for record in bundle.score_audit_records
        )
        batch = LearningObservationBatch(
            batch_id=f"observations_{bundle.attempt_id}_v{latest_version}",
            learner_id=bundle.learner_id,
            observations=[],
            watermark=f"{paper_id}:v{latest_version}",
            created_at=bundle.finalized_at,
        )
        self.store.observation_batches_built.append(batch)
        return batch.model_copy(deep=True)

    def finalize_scoring(self, **_: Any):
        self.store.scoring_calls += 1
        generated = _scoring_result(
            audit_id="audit_attempt_1",
        )
        self.store.scoring = generated.model_copy(
            update={
                "learner_id": "learner_1",
                "learning_events": [
                    event.model_copy(update={"learner_id": "learner_1"})
                    for event in generated.learning_events
                ],
                "remediation_plan": generated.remediation_plan.model_copy(
                    update={"learner_id": "learner_1"}
                ),
            }
        )
        self.store.scoring_history.append(self.store.scoring)
        self.store.trip("scoring")
        return self.store.scoring.model_copy(deep=True)

    def apply_teacher_review(self, teacher_review_decision, **_: Any):
        version = teacher_review_decision.expected_audit_version + 1
        generated = _scoring_result(
            audit_id="audit_attempt_1",
            audit_version=version,
            finalized_at=NOW + timedelta(minutes=version - 1),
        )
        self.store.reviewed = generated.model_copy(
            update={
                "learner_id": "learner_1",
                "learning_events": [
                    event.model_copy(update={"learner_id": "learner_1"})
                    for event in generated.learning_events
                ],
                "remediation_plan": generated.remediation_plan.model_copy(
                    update={"learner_id": "learner_1"}
                ),
            }
        )
        self.store.scoring_history.append(self.store.reviewed)
        self.store.trip("review")
        return self.store.reviewed.model_copy(deep=True)


class _M5:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def get_state_update(self, attempt_id: str):
        return None if self.store.state is None else self.store.state.model_copy(deep=True)

    def get_state_update_version(self, attempt_id: str, state_version: int):
        for value in self.store.state_history:
            if value.learner_state_snapshot.state_version == state_version:
                return value.model_copy(deep=True)
        return None

    def get_state_update_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ):
        key = f"{audit_id}:{audit_version}"
        for value in self.store.state_history:
            if key in value.processed_audit_ids:
                return value.model_copy(deep=True)
        return None

    def get_latest_learner_state(self, *_: Any):
        return None if self.store.state is None else self.store.state.learner_state_snapshot

    def get_latest_class_state(self, *_: Any):
        return None if self.store.state is None else self.store.state.class_state_snapshot

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ):
        for state in reversed(self.store.state_history):
            snapshot = state.learner_state_snapshot
            if (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.learner_id,
                snapshot.state_version,
            ) == (course_id, class_id, learner_id, state_version):
                return snapshot.model_copy(deep=True)
        return None

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ):
        for state in reversed(self.store.state_history):
            snapshot = state.class_state_snapshot
            if (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.snapshot_id,
            ) == (course_id, class_id, snapshot_id):
                return snapshot.model_copy(deep=True)
        return None

    def update_state(self, scoring_result_bundle, **kwargs: Any):
        observation_batch = kwargs.get("learning_observation_batch")
        if observation_batch is not None:
            self.store.observation_batches_received.append(
                observation_batch.model_copy(deep=True)
            )
        latest_version = max(
            record.audit_version
            for record in scoring_result_bundle.score_audit_records
        )
        generated = _state_update(
            audit_id="audit_attempt_1",
            audit_version=latest_version,
            state_version=latest_version,
            updated_at=scoring_result_bundle.finalized_at,
        )
        self.store.state = generated.model_copy(
            update={
                "diagnosis_result": generated.diagnosis_result.model_copy(
                    update={"learner_id": "learner_1"}
                ),
                "learner_state_snapshot": (
                    generated.learner_state_snapshot.model_copy(
                        update={"learner_id": "learner_1"}
                    )
                )
            }
        )
        self.store.state_history.append(self.store.state)
        self.store.trip(
            "review_state" if self.store.reviewed is not None else "state"
        )
        return self.store.state.model_copy(deep=True)

    def update_state_with_frozen_policy(
        self,
        *,
        expected_policy_checksum: str,
        **kwargs: Any,
    ):
        assert len(expected_policy_checksum) == 64
        return self.update_state(**kwargs)


class _BaselineM5(_M5):
    def __init__(self, store: _WorkflowStore) -> None:
        super().__init__(store)
        original = _state_update(
            state_version=7,
            updated_at=NOW - timedelta(minutes=2),
        )
        original = original.model_copy(
            update={
                "learner_state_snapshot": (
                    original.learner_state_snapshot.model_copy(
                        update={"learner_id": "learner_1"}
                    )
                )
            }
        )
        self.latest = original
        self.snapshots = {
            original.learner_state_snapshot.snapshot_id: original,
        }
        self.latest_learner_calls = 0
        self.latest_class_calls = 0
        self.exact_learner_calls = 0
        self.exact_class_calls = 0
        self.update_inputs: list[tuple[Any, Any]] = []
        self.fail_first_update = True

    def install_new_latest(self) -> None:
        newer = _state_update(
            state_version=8,
            updated_at=NOW - timedelta(minutes=1),
        )
        newer = newer.model_copy(
            update={
                "learner_state_snapshot": (
                    newer.learner_state_snapshot.model_copy(
                        update={"learner_id": "learner_1"}
                    )
                )
            }
        )
        self.latest = newer
        self.snapshots = {
            **self.snapshots,
            newer.learner_state_snapshot.snapshot_id: newer,
        }

    def get_latest_learner_state(self, *_: Any):
        self.latest_learner_calls += 1
        return self.latest.learner_state_snapshot.model_copy(deep=True)

    def get_latest_class_state(self, *_: Any):
        self.latest_class_calls += 1
        return self.latest.class_state_snapshot.model_copy(deep=True)

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ):
        self.exact_learner_calls += 1
        for result in self.snapshots.values():
            snapshot = result.learner_state_snapshot
            if (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.learner_id,
                snapshot.state_version,
            ) == (course_id, class_id, learner_id, state_version):
                return snapshot.model_copy(deep=True)
        return None

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ):
        self.exact_class_calls += 1
        for result in self.snapshots.values():
            snapshot = result.class_state_snapshot
            if (
                snapshot.course_id,
                snapshot.class_id,
                snapshot.snapshot_id,
            ) == (course_id, class_id, snapshot_id):
                return snapshot.model_copy(deep=True)
        return None

    def update_state(
        self,
        *,
        previous_learner_state_snapshot,
        previous_class_state_snapshot,
        **kwargs: Any,
    ):
        self.update_inputs.append(
            (
                previous_learner_state_snapshot,
                previous_class_state_snapshot,
            )
        )
        if self.fail_first_update:
            self.fail_first_update = False
            raise RuntimeError("injected failure after baseline freeze")
        return super().update_state(**kwargs)


class _PostPersistFailingBaselineM5(_BaselineM5):
    """Fail once after the authoritative M5 result has already been saved."""

    def __init__(self, store: _WorkflowStore) -> None:
        super().__init__(store)
        self.fail_first_update = False
        self.fail_after_persist = True

    def update_state(
        self,
        *,
        previous_learner_state_snapshot,
        previous_class_state_snapshot,
        **kwargs: Any,
    ):
        self.update_inputs.append(
            (
                previous_learner_state_snapshot,
                previous_class_state_snapshot,
            )
        )
        result = _M5.update_state(self, **kwargs)
        if self.fail_after_persist:
            self.fail_after_persist = False
            raise RuntimeError("injected failure after state persistence")
        return result


class _M6:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def prepare_policy_execution(self, **_: Any) -> PolicyExecutionRef:
        execution = self.store.policy_execution
        self.store.prepare_results.append(execution)
        self.store.policy_events.append("prepare")
        self.store.trip("policy_binding")
        return execution

    def decide_next_action(self, **_: Any):
        self.store.tutoring_calls += 1
        self.store.policy_events.append("decide")
        if self.store.tutoring is None:
            self.store.tutoring = _decide(_complete_inputs())
        self.store.trip("tutoring")
        return self.store.tutoring.model_copy(deep=True)


class _M7:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def score_subjective_answer(self, rubric_scoring_task, **_: Any):
        self.store.scored_task_ids.append(rubric_scoring_task.scoring_task_id)
        return SimpleNamespace(scoring_task_id=rubric_scoring_task.scoring_task_id)

    def get_feedback_for_task(self, *_: Any):
        return (
            None
            if self.store.feedback is None
            else self.store.feedback.model_copy(deep=True)
        )

    def get_feedback(self, *_: Any):
        return self.get_feedback_for_task()

    def generate_student_feedback(self, **_: Any):
        self.store.feedback = _feedback()
        self.store.trip("feedback")
        return self.store.feedback.model_copy(deep=True)


class _M9:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def get_analytics(self, report_id: str | None = None):
        value = (
            self.store.analytics
            if report_id is None
            else self.store.analytics_history.get(report_id)
        )
        return None if value is None else value.model_copy(deep=True)

    def get_latest_analytics(self, **_: Any):
        return self.get_analytics()

    def build_teacher_analytics(self, **_: Any):
        state = self.store.state
        self.store.analytics = _analytics(
            report_id=(
                f"report_course_1_{state.class_state_snapshot.snapshot_id}"
            ),
            generated_at=state.updated_at,
        )
        self.store.analytics_history[
            self.store.analytics.report_id
        ] = self.store.analytics
        self.store.trip(
            "review_analytics"
            if self.store.reviewed is not None
            else "analytics"
        )
        return self.store.analytics.model_copy(deep=True)

    def build_teacher_analytics_with_frozen_policy(
        self,
        *,
        expected_policy_checksum: str,
        **kwargs: Any,
    ):
        assert len(expected_policy_checksum) == 64
        return self.build_teacher_analytics(**kwargs)

    def get_review_decision(self, decision_id: str):
        value = self.store.reviews.get(decision_id)
        return None if value is None else value.model_copy(deep=True)

    def record_teacher_review(self, raw_review_path, **_: Any):
        self.store.review = TeacherReviewDecision(
            decision_id=raw_review_path.submission_id,
            audit_id=raw_review_path.audit_id,
            expected_audit_version=raw_review_path.expected_audit_version,
            decision=raw_review_path.decision,
            final_total_score=raw_review_path.final_total_score,
            criterion_overrides=raw_review_path.criterion_overrides,
            teacher_comment=raw_review_path.teacher_comment,
            reviewer_id=raw_review_path.reviewer_id,
            reviewed_at=raw_review_path.submitted_at,
        )
        self.store.reviews[self.store.review.decision_id] = self.store.review
        self.store.trip("decision")
        return self.store.review.model_copy(deep=True)


class _M2:
    def __init__(self, store: _WorkflowStore) -> None:
        self.store = store

    def retrieve(self, *, evidence_query, **_: Any):
        self.store.retrieved_queries.append(
            (
                getattr(evidence_query, "use_case", None),
                getattr(evidence_query, "query_id", None),
            )
        )
        return SimpleNamespace()


def _preparation(task_ids: list[str]):
    queries = {
        task_id: SimpleNamespace(
            query_id=f"query_{task_id}",
            use_case="grading",
        )
        for task_id in task_ids
    }
    return SimpleNamespace(
        rubric_scoring_tasks=[
            SimpleNamespace(scoring_task_id=task_id) for task_id in task_ids
        ],
        query_for_task=lambda task_id: queries[task_id],
    )


def _policy_execution(
    *,
    mode: str = "rules",
    artifact_sha256: str | None = None,
) -> PolicyExecutionRef:
    learned = mode != "rules"
    return PolicyExecutionRef(
        request_fingerprint="f" * 64,
        mode=mode,
        policy_id=(
            "learned-policy-v1" if learned else "m6-deterministic-v1"
        ),
        adapter_id=(
            "linucb-adapter" if learned else "m6-rules-adapter"
        ),
        adapter_version="v1",
        artifact_sha256=artifact_sha256,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        gate_policy_version="m6-active-gate-v1",
    )


class _M0FaultProxy:
    def __init__(
        self,
        delegate: M0PlatformService,
        store: _WorkflowStore,
    ) -> None:
        self.delegate = delegate
        self.store = store

    def append_learning_events(self, events):
        result = self.delegate.append_learning_events(events)
        self.store.trip("events")
        return result

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


def _coordinator(
    tmp_path: Path,
    store: _WorkflowStore,
    *,
    m6: Any | None = None,
    m5: Any | None = None,
) -> AppCoordinator:
    _ensure_policy_files(tmp_path)
    m0 = M0PlatformService(
        database_path=tmp_path / "workflow.db",
        runtime_dir=tmp_path / "runtime",
        config_dir=tmp_path / "config",
    )
    m0.initialize()
    unused = SimpleNamespace()
    return AppCoordinator(
        m0_service=_M0FaultProxy(m0, store),
        m1_service=unused,
        m2_service=_M2(store),
        m3_service=unused,
        m4_service=_M4(store),
        m5_service=m5 or _M5(store),
        m6_service=m6 or _M6(store),
        m7_service=_M7(store),
        m8_service=_M8(store),
        m9_service=_M9(store),
    )


def _ensure_policy_files(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    teacher_path = tmp_path / "teacher.json"
    if not state_path.exists():
        state_path.write_text(
            json.dumps(
                {
                    "aggregation_policy_version": "1.0.0",
                    "class_id": "class_1",
                    "class_size": 1,
                    "consolidating_threshold": 0.5,
                    "mastered_threshold": 0.8,
                    "minimum_assessed_count": 1,
                    "minimum_coverage": 1.0,
                    "misconception_activation_threshold": 0.5,
                }
            ),
            encoding="utf-8",
        )
    if not teacher_path.exists():
        teacher_path.write_text(
            json.dumps(
                {
                    "minimum_coverage": 1.0,
                    "minimum_assessed_count": 1,
                    "minimum_confidence": 0.5,
                    "weak_mastery_threshold": 0.8,
                    "misconception_threshold": 0.5,
                    "priority_support_threshold": 0.5,
                }
            ),
            encoding="utf-8",
        )


def test_split_assessment_use_cases_reload_and_review(tmp_path: Path) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    knowledge = _knowledge_bundle()

    started = coordinator.start_assessment(
        student_text="start stage assessment",
        task_type_hint="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        knowledge_bundle=knowledge,
    )
    assert set(started) == {"task_plan", "assessment_paper"}
    pending = _coordinator(tmp_path, store).get_pending_assessment(
        paper_id="paper_1",
        learner_id="learner_1",
    )
    assert pending == started

    with pytest.raises(DomainError) as pending_scope_error:
        coordinator.get_pending_assessment(
            paper_id="paper_1",
            learner_id="other_learner",
        )
    assert pending_scope_error.value.code == "ASSESSMENT_NOT_FOUND"

    submission = AssessmentSubmission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        answers={"item_1": "governed"},
        submitted_at=NOW,
    )
    submitted = coordinator.submit_assessment(
        assessment_submission=submission,
        request_id="submit_request_1",
        index_ref=_ready_index_ref(),
        knowledge_bundle=knowledge,
        state_policy_path=tmp_path / "state.json",
        teacher_threshold_policy_path=tmp_path / "teacher.json",
    )
    assert {
        "task_plan",
        "assessment_paper",
        "scoring_result",
        "state_result",
        "tutoring_result",
        "feedback",
        "analytics",
    } <= set(submitted)
    assert [
        batch.batch_id for batch in store.observation_batches_built
    ] == ["observations_attempt_1_v1"]
    assert store.observation_batches_received == store.observation_batches_built
    with pytest.raises(DomainError) as submitted_pending_error:
        coordinator.get_pending_assessment(
            paper_id="paper_1",
            learner_id="learner_1",
        )
    assert (
        submitted_pending_error.value.code
        == "ASSESSMENT_ALREADY_SUBMITTED"
    )

    restarted = _coordinator(tmp_path, store)
    student = restarted.get_student_assessment(
        paper_id="paper_1",
        learner_id="learner_1",
    )
    teacher = restarted.get_teacher_review_context(
        paper_id="paper_1",
        course_id="course_1",
        class_id="class_1",
    )
    assert student["assessment_paper"].paper_id == "paper_1"
    assert student["feedback"].feedback_id == "feedback_1"
    assert set(teacher) == {
        "assessment_paper",
        "scoring_result",
        "state_result",
        "analytics",
    }
    assert teacher["assessment_paper"].paper_id == "paper_1"

    review_submission = TeacherReviewSubmission(
        submission_id="decision_1",
        audit_id="audit_attempt_1",
        expected_audit_version=1,
        reviewer_id="teacher_1",
        decision="confirm",
        final_total_score=0.0,
        criterion_overrides=[],
        teacher_comment="Confirmed against governed evidence.",
        submitted_at=NOW + timedelta(minutes=1),
    )
    reviewed = restarted.review_assessment(
        paper_id="paper_1",
        review_submission=review_submission,
        request_id="review_request_1",
        knowledge_bundle=knowledge,
        state_policy_path=tmp_path / "state.json",
        teacher_threshold_policy_path=tmp_path / "teacher.json",
        course_id="course_1",
        class_id="class_1",
    )
    assert set(reviewed) == {
        "review_decision",
        "reviewed_scoring_result",
        "recomputed_state_result",
        "refreshed_analytics",
    }
    assert [
        batch.batch_id for batch in store.observation_batches_built
    ] == ["observations_attempt_1_v1", "observations_attempt_1_v2"]
    assert store.observation_batches_received == store.observation_batches_built
    refreshed_context = restarted.get_teacher_review_context(
        paper_id="paper_1",
        course_id="course_1",
        class_id="class_1",
    )
    assert (
        refreshed_context["analytics"].report_id
        == reviewed["refreshed_analytics"].report_id
    )
    assert (
        refreshed_context["analytics"].report_id
        != submitted["analytics"].report_id
    )


def test_legacy_review_cycle_passes_frozen_observations_to_m5(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    current = _scoring_result(audit_id="audit_attempt_1").model_copy(
        update={"learner_id": "learner_1"},
        deep=True,
    )
    previous_state = _state_update(
        audit_id="audit_attempt_1",
        audit_version=1,
        state_version=1,
    )
    review = TeacherReviewSubmission(
        submission_id="legacy_review_1",
        audit_id="audit_attempt_1",
        expected_audit_version=1,
        reviewer_id="teacher_1",
        decision="confirm",
        final_total_score=0.0,
        criterion_overrides=[],
        teacher_comment="Confirmed.",
        submitted_at=NOW + timedelta(minutes=1),
    )

    result = coordinator.run_teacher_review_cycle(
        knowledge_bundle=_knowledge_bundle(),
        scoring_result_bundle=current,
        state_update_result=previous_state,
        raw_review_path=review,
        state_policy_path=tmp_path / "state.json",
        teacher_threshold_policy_path=tmp_path / "teacher.json",
    )

    assert result["reviewed_scoring_result"].attempt_id == current.attempt_id
    assert [
        batch.batch_id for batch in store.observation_batches_received
    ] == ["observations_attempt_1_v2"]


def test_observation_builder_compatibility_fallback_returns_none(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    scoring = _scoring_result(audit_id="audit_attempt_1")
    coordinator._m8 = SimpleNamespace()
    coordinator._assessment_workflow._m8 = SimpleNamespace()

    assert coordinator._build_observation_batch(scoring) is None
    assert (
        coordinator._assessment_workflow._build_observation_batch(scoring)
        is None
    )


def _start_and_submission(
    coordinator: AppCoordinator,
    tmp_path: Path,
):
    knowledge = _knowledge_bundle()
    coordinator.start_assessment(
        student_text="start stage assessment",
        task_type_hint="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        knowledge_bundle=knowledge,
    )
    submission = AssessmentSubmission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        answers={"item_1": "governed"},
        submitted_at=NOW,
    )
    arguments = {
        "assessment_submission": submission,
        "request_id": "submit_request_1",
        "index_ref": _ready_index_ref(),
        "knowledge_bundle": knowledge,
        "state_policy_path": tmp_path / "state.json",
        "teacher_threshold_policy_path": tmp_path / "teacher.json",
    }
    return knowledge, submission, arguments


@pytest.mark.parametrize(
    ("failure_point", "checkpoint"),
    [
        ("scoring", "claimed"),
        ("events", "scoring_saved"),
        ("state", "state_inputs_frozen"),
        ("feedback", "tutoring_saved"),
        ("analytics", "feedback_saved"),
    ],
)
def test_submit_recovers_after_module_save_before_checkpoint(
    tmp_path: Path,
    failure_point: str,
    checkpoint: str,
) -> None:
    store = _WorkflowStore()
    first = _coordinator(tmp_path, store)
    _, submission, arguments = _start_and_submission(first, tmp_path)
    store.fail_after = failure_point

    with pytest.raises(DomainError) as captured:
        first.submit_assessment(**arguments)
    assert captured.value.code == "WORKFLOW_EXECUTION_FAILED"
    failed = first._m0.get_assessment_run("submit:submission_1")
    assert failed.status == "failed"
    assert failed.checkpoint == checkpoint
    assert failed.locked_by is None

    recovered = _coordinator(tmp_path, store).submit_assessment(**arguments)
    replayed = _coordinator(tmp_path, store).submit_assessment(**arguments)
    assert recovered == replayed
    assert recovered["scoring_result"].attempt_id == submission.attempt_id


def test_submit_freezes_rules_policy_before_deciding(tmp_path: Path) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    coordinator.submit_assessment(**arguments)

    run = coordinator._m0.get_assessment_run("submit:submission_1")
    assert run.policy_id == "m6-deterministic-v1"
    assert run.adapter_id == "m6-rules-adapter"
    assert run.adapter_version == "v1"
    assert run.artifact_sha256 is None
    assert run.feature_schema_version == "m6-features-v1"
    assert run.action_space_version == "m6-action-space-v1"
    assert run.gate_policy_version == "m6-active-gate-v1"
    assert store.policy_events[:2] == ["prepare", "decide"]


def test_submit_recovers_first_writer_after_binding_before_checkpoint(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    first = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(first, tmp_path)
    store.fail_after = "policy_binding"

    with pytest.raises(DomainError) as captured:
        first.submit_assessment(**arguments)
    assert captured.value.code == "WORKFLOW_EXECUTION_FAILED"
    failed = first._m0.get_assessment_run("submit:submission_1")
    assert failed.checkpoint == "state_saved"
    assert failed.policy_id is None

    recovered = _coordinator(tmp_path, store).submit_assessment(**arguments)
    stored = first._m0.get_assessment_run("submit:submission_1")

    assert recovered["tutoring_result"] is not None
    assert len(store.prepare_results) == 2
    assert store.prepare_results[0] == store.prepare_results[1]
    assert stored.policy_id == "m6-deterministic-v1"


def test_submit_recovery_requires_exact_frozen_policy_before_deciding(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    first = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(first, tmp_path)
    store.fail_after = "tutoring"

    with pytest.raises(DomainError):
        first.submit_assessment(**arguments)
    frozen = first._m0.get_assessment_run("submit:submission_1")
    assert frozen.checkpoint == "policy_frozen"
    calls_before_recovery = store.tutoring_calls
    store.policy_execution = _policy_execution(
        mode="active",
        artifact_sha256="e" * 64,
    )

    with pytest.raises(DomainError) as captured:
        _coordinator(tmp_path, store).submit_assessment(**arguments)

    assert captured.value.code == "WORKFLOW_DEPENDENCY_MISMATCH"
    assert store.tutoring_calls == calls_before_recovery


def test_submit_rejects_learned_policy_without_artifact_sha(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    store.policy_execution = _policy_execution(mode="active")
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    with pytest.raises(DomainError) as captured:
        coordinator.submit_assessment(**arguments)

    assert captured.value.code == "WORKFLOW_DEPENDENCY_MISMATCH"
    run = coordinator._m0.get_assessment_run("submit:submission_1")
    assert run.checkpoint == "state_saved"
    assert run.policy_id is None
    assert store.tutoring_calls == 0


def test_submit_narrowly_adopts_pre_v11_post_policy_checkpoint(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)
    expected = coordinator.submit_assessment(**arguments)
    with connect_sqlite(tmp_path / "workflow.db") as connection:
        connection.execute(
            """
            UPDATE m0_assessment_runs
            SET checkpoint = 'tutoring_saved',
                status = 'failed',
                locked_by = NULL,
                lease_until = NULL,
                error_code = 'LEGACY_V10_FAILURE',
                policy_id = NULL,
                adapter_id = NULL,
                adapter_version = NULL,
                artifact_sha256 = NULL,
                feature_schema_version = NULL,
                action_space_version = NULL,
                gate_policy_version = NULL,
                version = version + 1
            WHERE operation_id = 'submit:submission_1'
            """
        )

    recovered = _coordinator(tmp_path, store).submit_assessment(**arguments)
    adopted = coordinator._m0.get_assessment_run("submit:submission_1")

    assert recovered == expected
    assert adopted.status == "completed"
    assert adopted.policy_id == "m6-deterministic-v1"
    assert adopted.artifact_sha256 is None


@pytest.mark.parametrize(
    ("failure_point", "checkpoint"),
    [
        ("decision", "claimed"),
        ("review", "decision_saved"),
        ("review_state", "state_inputs_frozen"),
    ],
)
def test_review_recovers_after_authoritative_save(
    tmp_path: Path,
    failure_point: str,
    checkpoint: str,
) -> None:
    store = _WorkflowStore()
    first = _coordinator(tmp_path, store)
    knowledge, _, submit_arguments = _start_and_submission(first, tmp_path)
    first.submit_assessment(**submit_arguments)
    review = TeacherReviewSubmission(
        submission_id="decision_1",
        audit_id="audit_attempt_1",
        expected_audit_version=1,
        reviewer_id="teacher_1",
        decision="confirm",
        final_total_score=0.0,
        criterion_overrides=[],
        teacher_comment="Confirmed against governed evidence.",
        submitted_at=NOW + timedelta(minutes=1),
    )
    arguments = {
        "paper_id": "paper_1",
        "review_submission": review,
        "request_id": "review_request_1",
        "knowledge_bundle": knowledge,
        "state_policy_path": tmp_path / "state.json",
        "teacher_threshold_policy_path": tmp_path / "teacher.json",
        "course_id": "course_1",
        "class_id": "class_1",
    }
    store.fail_after = failure_point

    with pytest.raises(DomainError) as captured:
        first.review_assessment(**arguments)
    assert captured.value.code == "WORKFLOW_EXECUTION_FAILED"
    failed = first._m0.get_assessment_run("review:decision_1")
    assert failed.status == "failed"
    assert failed.checkpoint == checkpoint

    recovered = _coordinator(tmp_path, store).review_assessment(**arguments)
    replayed = _coordinator(tmp_path, store).review_assessment(**arguments)
    assert recovered == replayed
    assert (
        recovered["reviewed_scoring_result"]
        .get_audit_record("audit_attempt_1")
        .audit_version
        == 2
    )


def test_submit_replay_uses_submission_identity_not_correlation_id(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, submission, arguments = _start_and_submission(coordinator, tmp_path)
    first = coordinator.submit_assessment(**arguments)

    replay = coordinator.submit_assessment(
        **{
            **arguments,
            "request_id": "different_http_correlation_id",
        }
    )
    assert replay == first

    conflicting = submission.model_copy(
        update={
            "submission_id": "submission_2",
            "answers": {"item_1": "different"},
        }
    )
    with pytest.raises(DomainError) as captured:
        coordinator.submit_assessment(
            **{
                **arguments,
                "assessment_submission": conflicting,
                "request_id": "another_http_request",
            }
        )
    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"


def test_submit_adopts_pre_v9_start_and_failed_inflight_run(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, submission, arguments = _start_and_submission(coordinator, tmp_path)
    database_path = tmp_path / "workflow.db"
    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            UPDATE m0_assessment_runs
            SET knowledge_bundle_id = NULL,
                knowledge_bundle_version = NULL,
                knowledge_bundle_checksum = NULL,
                course_package_id = NULL
            WHERE operation = 'start' AND paper_id = 'paper_1'
            """
        )
    coordinator._m0.record_assessment_run(
        AssessmentRun(
            operation_id="submit:submission_1",
            operation="submit",
            request_checksum=submission.content_checksum(),
            course_id="course_1",
            class_id="class_1",
            learner_id="learner_1",
            session_id="session_1",
            task_id="task_1",
            paper_id="paper_1",
            attempt_id="attempt_1",
            feedback_id=None,
            report_id=None,
            checkpoint="claimed",
            status="failed",
            version=2,
            locked_by=None,
            lease_until=None,
            error_code="SAFE_LEGACY_FAILURE",
            created_at=NOW,
            updated_at=NOW,
        )
    )

    result = coordinator.submit_assessment(**arguments)

    adopted_start = coordinator._m0.get_assessment_run_by_paper(
        "paper_1",
        operation="start",
    )
    adopted_submit = coordinator._m0.get_assessment_run(
        "submit:submission_1"
    )
    assert result["state_result"] is not None
    assert adopted_start.knowledge_bundle_id is not None
    assert adopted_submit.status == "completed"
    assert adopted_submit.knowledge_bundle_id is not None
    assert adopted_submit.evidence_index_id is not None
    assert adopted_submit.state_policy_checksum is not None
    assert adopted_submit.teacher_policy_checksum is not None
    assert adopted_submit.previous_state_frozen is True


def test_result_and_teacher_scope_checks_fail_closed(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, _WorkflowStore())
    _, _, arguments = _start_and_submission(coordinator, tmp_path)
    coordinator.submit_assessment(**arguments)

    with pytest.raises(DomainError) as student_error:
        coordinator.get_student_assessment(
            paper_id="paper_1",
            learner_id="other_learner",
        )
    assert student_error.value.code == "ASSESSMENT_NOT_FOUND"

    with pytest.raises(DomainError) as teacher_error:
        coordinator.get_teacher_review_context(
            paper_id="paper_1",
            course_id="other_course",
            class_id="class_1",
        )
    assert teacher_error.value.code == "ASSESSMENT_SCOPE_MISMATCH"


def test_completed_submit_replays_real_m6_request_without_new_turn(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    repository = _sqlite_m6_repository(tmp_path / "m6.db")
    m6 = _RecordingM6(
        M6TutoringControlService(DEFAULT_STATE_MACHINE, repository)
    )
    coordinator = _coordinator(tmp_path, store, m6=m6)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    first = coordinator.submit_assessment(**arguments)
    second = coordinator.submit_assessment(
        **{**arguments, "request_id": "new_correlation"}
    )

    assert first["tutoring_result"] == second["tutoring_result"]
    assert len(m6.results) == 2
    assert (
        m6.results[0].session_state_snapshot.turn_count
        == m6.results[1].session_state_snapshot.turn_count
    )


def test_concurrent_duplicate_posts_never_compute_two_scores(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)
    barrier = Barrier(2)
    results: list[dict[str, Any]] = []
    error_codes: list[str] = []
    lock = Lock()

    def submit(correlation_id: str) -> None:
        barrier.wait()
        try:
            result = coordinator.submit_assessment(
                **{**arguments, "request_id": correlation_id}
            )
            with lock:
                results.append(result)
        except DomainError as error:
            with lock:
                error_codes.append(error.code)

    threads = [
        Thread(target=submit, args=("correlation_a",)),
        Thread(target=submit, args=("correlation_b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.scoring_calls == 1
    assert results
    assert all(result == results[0] for result in results)
    assert set(error_codes) <= {"WORKFLOW_BUSY"}


def _review_submission(
    *,
    decision_id: str,
    expected_version: int,
) -> TeacherReviewSubmission:
    return TeacherReviewSubmission(
        submission_id=decision_id,
        audit_id="audit_attempt_1",
        expected_audit_version=expected_version,
        reviewer_id="teacher_1",
        decision="confirm",
        final_total_score=0.0,
        criterion_overrides=[],
        teacher_comment="Confirmed against governed evidence.",
        submitted_at=NOW + timedelta(minutes=expected_version),
    )


def test_failed_review_remains_invisible_to_student_and_teacher_reads(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    knowledge, _, submit_arguments = _start_and_submission(
        coordinator,
        tmp_path,
    )
    submitted = coordinator.submit_assessment(**submit_arguments)
    store.fail_after = "review"

    with pytest.raises(DomainError):
        coordinator.review_assessment(
            paper_id="paper_1",
            review_submission=_review_submission(
                decision_id="failed_decision",
                expected_version=1,
            ),
            request_id="failed_review_request",
            knowledge_bundle=knowledge,
            state_policy_path=tmp_path / "state.json",
            teacher_threshold_policy_path=tmp_path / "teacher.json",
            course_id="course_1",
            class_id="class_1",
        )

    student = coordinator.get_student_assessment(
        paper_id="paper_1",
        learner_id="learner_1",
    )
    teacher = coordinator.get_teacher_review_context(
        paper_id="paper_1",
        course_id="course_1",
        class_id="class_1",
    )
    assert (
        student["scoring_result"]
        .get_audit_record("audit_attempt_1")
        .audit_version
        == 1
    )
    assert teacher["scoring_result"] == submitted["scoring_result"]
    assert teacher["analytics"] == submitted["analytics"]


def test_old_completed_review_replays_its_exact_historical_versions(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    knowledge, _, submit_arguments = _start_and_submission(
        coordinator,
        tmp_path,
    )
    coordinator.submit_assessment(**submit_arguments)

    def review_arguments(decision_id: str, expected_version: int):
        return {
            "paper_id": "paper_1",
            "review_submission": _review_submission(
                decision_id=decision_id,
                expected_version=expected_version,
            ),
            "request_id": f"correlation_{decision_id}",
            "knowledge_bundle": knowledge,
            "state_policy_path": tmp_path / "state.json",
            "teacher_threshold_policy_path": tmp_path / "teacher.json",
            "course_id": "course_1",
            "class_id": "class_1",
        }

    first_arguments = review_arguments("decision_1", 1)
    first = coordinator.review_assessment(**first_arguments)
    coordinator.review_assessment(**review_arguments("decision_2", 2))
    replay = coordinator.review_assessment(
        **{**first_arguments, "request_id": "later_correlation"}
    )

    assert replay == first
    assert (
        replay["reviewed_scoring_result"]
        .get_audit_record("audit_attempt_1")
        .audit_version
        == 2
    )
    assert replay["recomputed_state_result"].learner_state_snapshot.state_version == 2


def test_submit_supports_multiple_subjective_tasks_in_stable_order(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    store.preparation = _preparation(["rubric_task_1", "rubric_task_2"])
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    coordinator.submit_assessment(**arguments)

    assert store.scored_task_ids == ["rubric_task_1", "rubric_task_2"]
    assert [
        query_id
        for use_case, query_id in store.retrieved_queries
        if use_case == "grading"
    ] == ["query_rubric_task_1", "query_rubric_task_2"]


def test_submit_allows_objective_only_preparation_without_subjective_calls(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    store.preparation = _preparation([])
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    result = coordinator.submit_assessment(**arguments)

    assert result["scoring_result"].attempt_id == "attempt_1"
    assert store.scored_task_ids == []
    assert [
        query_id
        for use_case, query_id in store.retrieved_queries
        if use_case == "grading"
    ] == []


def test_new_review_is_blocked_while_prior_review_for_same_paper_is_incomplete(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    knowledge, _, submit_arguments = _start_and_submission(coordinator, tmp_path)
    coordinator.submit_assessment(**submit_arguments)
    store.fail_after = "review"

    with pytest.raises(DomainError):
        coordinator.review_assessment(
            paper_id="paper_1",
            review_submission=_review_submission(
                decision_id="decision_incomplete",
                expected_version=1,
            ),
            request_id="review_request_incomplete",
            knowledge_bundle=knowledge,
            state_policy_path=tmp_path / "state.json",
            teacher_threshold_policy_path=tmp_path / "teacher.json",
            course_id="course_1",
            class_id="class_1",
        )

    store.fail_after = None
    with pytest.raises(DomainError) as blocked:
        coordinator.review_assessment(
            paper_id="paper_1",
            review_submission=_review_submission(
                decision_id="decision_competing",
                expected_version=1,
            ),
            request_id="review_request_competing",
            knowledge_bundle=knowledge,
            state_policy_path=tmp_path / "state.json",
            teacher_threshold_policy_path=tmp_path / "teacher.json",
            course_id="course_1",
            class_id="class_1",
        )

    assert blocked.value.code == "WORKFLOW_BUSY"


def test_submit_replay_rejects_changed_policy_dependency(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)
    store.fail_after = "scoring"
    with pytest.raises(DomainError):
        coordinator.submit_assessment(**arguments)
    state_path = tmp_path / "state.json"
    state_path.write_text(
        state_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as captured:
        _coordinator(tmp_path, store).submit_assessment(**arguments)

    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"


def test_submit_rejects_bundle_identity_different_from_start(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path, _WorkflowStore())
    knowledge, _, arguments = _start_and_submission(coordinator, tmp_path)
    changed = knowledge.model_copy(update={"bundle_version": "2.0.0"})

    with pytest.raises(DomainError) as captured:
        coordinator.submit_assessment(
            **{
                **arguments,
                "knowledge_bundle": changed,
            }
        )

    assert captured.value.code == "WORKFLOW_DEPENDENCY_MISMATCH"


def test_submit_recovery_uses_frozen_state_baseline_not_latest(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    m5 = _BaselineM5(store)
    coordinator = _coordinator(tmp_path, store, m5=m5)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    with pytest.raises(DomainError) as captured:
        coordinator.submit_assessment(**arguments)
    assert captured.value.code == "WORKFLOW_EXECUTION_FAILED"
    failed = coordinator._m0.get_assessment_run("submit:submission_1")
    assert failed.checkpoint == "state_inputs_frozen"
    assert failed.previous_state_frozen is True
    assert failed.previous_learner_snapshot_id == "learner_snapshot_7"
    assert failed.previous_learner_state_version == 7
    assert failed.previous_class_snapshot_id == "class_snapshot_7"
    latest_calls = (m5.latest_learner_calls, m5.latest_class_calls)
    m5.install_new_latest()

    recovered = _coordinator(tmp_path, store, m5=m5).submit_assessment(
        **arguments
    )

    assert recovered["state_result"] is not None
    assert (m5.latest_learner_calls, m5.latest_class_calls) == latest_calls
    assert m5.exact_learner_calls >= 1
    assert m5.exact_class_calls >= 1
    previous_learner, previous_class = m5.update_inputs[-1]
    assert previous_learner.snapshot_id == "learner_snapshot_7"
    assert previous_class.snapshot_id == "class_snapshot_7"


def test_submit_recovery_after_state_persist_does_not_refreeze_latest(
    tmp_path: Path,
) -> None:
    store = _WorkflowStore()
    m5 = _PostPersistFailingBaselineM5(store)
    coordinator = _coordinator(tmp_path, store, m5=m5)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)

    with pytest.raises(DomainError) as captured:
        coordinator.submit_assessment(**arguments)

    assert captured.value.code == "WORKFLOW_EXECUTION_FAILED"
    failed = coordinator._m0.get_assessment_run("submit:submission_1")
    assert failed.checkpoint == "state_inputs_frozen"
    assert failed.previous_state_frozen is True
    assert failed.previous_learner_snapshot_id == "learner_snapshot_7"
    assert failed.previous_class_snapshot_id == "class_snapshot_7"
    assert len(store.state_history) == 1
    assert len(m5.update_inputs) == 1
    latest_calls = (m5.latest_learner_calls, m5.latest_class_calls)
    m5.install_new_latest()

    recovered = _coordinator(tmp_path, store, m5=m5).submit_assessment(
        **arguments
    )

    assert recovered["state_result"] == store.state
    assert (m5.latest_learner_calls, m5.latest_class_calls) == latest_calls
    assert len(m5.update_inputs) == 1
    previous_learner, previous_class = m5.update_inputs[0]
    assert previous_learner.snapshot_id == "learner_snapshot_7"
    assert previous_class.snapshot_id == "class_snapshot_7"


class _LeaseLosingM0:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.renew_calls = 0

    def renew_assessment_run_lease(self, *args: Any, **kwargs: Any) -> bool:
        self.renew_calls += 1
        return self.renew_calls == 1

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


class _SlowM2(_M2):
    def retrieve(self, **kwargs: Any):
        time.sleep(0.04)
        return super().retrieve(**kwargs)


def test_submit_stops_after_workflow_lease_is_lost(tmp_path: Path) -> None:
    store = _WorkflowStore()
    coordinator = _coordinator(tmp_path, store)
    _, _, arguments = _start_and_submission(coordinator, tmp_path)
    lease_losing_m0 = _LeaseLosingM0(coordinator._assessment_workflow._m0)
    coordinator._assessment_workflow._m0 = lease_losing_m0
    coordinator._assessment_workflow._recovery = AssessmentRecovery(
        m0=lease_losing_m0,
        m5=coordinator._assessment_workflow._m5,
        now=coordinator._assessment_workflow._now,
    )
    coordinator._assessment_workflow._m2 = _SlowM2(store)
    coordinator._assessment_workflow._heartbeat_interval_seconds = 0.005

    with pytest.raises(DomainError) as captured:
        coordinator.submit_assessment(**arguments)

    assert captured.value.code == "WORKFLOW_LEASE_LOST"
    assert lease_losing_m0.renew_calls >= 1
    assert store.scoring_calls == 0
