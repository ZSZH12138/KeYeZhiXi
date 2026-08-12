"""Integration tests for the persisted IRT approval and ability workflow."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTItemParameters,
    IRTParameterSet,
    LearningObservation,
    LearningObservationBatch,
    ModelQualityReport,
)
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService


NOW = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)


def _shadow_parameter_set() -> IRTParameterSet:
    return IRTParameterSet(
        parameter_set_id="irt_shadow_task9",
        model_type="2PL",
        version="irt-task9-v1",
        item_parameters=[
            IRTItemParameters(
                item_id=f"item_{index}",
                item_version="1.0.0",
                discrimination=1.2 + index * 0.1,
                difficulty=-0.5 + index * 0.5,
                guessing=0.0,
                sample_size=240,
            )
            for index in range(3)
        ],
        sample_size=240,
        status="shadow",
        created_at=NOW,
    )


def _shadow_run() -> CalibrationRunResult:
    return CalibrationRunResult(
        run_id="calibration_task9",
        parameter_set=_shadow_parameter_set(),
        converged=True,
        metrics={"log_likelihood": -120.0, "iteration_count": 18.0},
        status="shadow",
        failure_code=None,
        generated_at=NOW,
    )


def _quality(run: CalibrationRunResult, *, status: str = "ready") -> ModelQualityReport:
    return ModelQualityReport(
        report_id=f"quality_{status}",
        subject_ref=run.run_id,
        metrics=(
            {"parameter_stability": 0.96, "sample_coverage": 1.0}
            if status == "ready"
            else {}
        ),
        observation_count=run.parameter_set.sample_size,
        status=status,
        generated_at=NOW + timedelta(minutes=1),
    )


def _decision(
    run: CalibrationRunResult,
    *,
    decision: str,
    decision_id: str | None = None,
) -> CalibrationReviewDecision:
    return CalibrationReviewDecision(
        decision_id=decision_id or f"decision_{decision}",
        calibration_run_id=run.run_id,
        reviewer_id="teacher_1",
        decision=decision,
        target_parameter_version=run.parameter_set.version,
        reason=f"teacher chose {decision}",
        reviewed_at=NOW + timedelta(minutes=2),
    )


def _repository(database_path: Path) -> SQLiteM8Repository:
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    return repository


def _service(repository: SQLiteM8Repository, *, calibrator=None) -> M8AssessmentService:
    return M8AssessmentService(
        repository=repository,
        rule_scorer=object(),
        parameter_item_generator=object(),
        irt_calibrator=calibrator,
    )


def _responses(*, learner_id: str, correct: bool) -> list[LearningObservation]:
    return [
        LearningObservation(
            observation_id=f"obs_{learner_id}_{index}",
            learner_id=learner_id,
            course_id="course_1",
            class_id="class_1",
            attempt_id=f"attempt_{learner_id}",
            item_id=f"item_{index}",
            item_version="1.0.0",
            concept_ids=["concept_1"],
            score=1.0 if correct else 0.0,
            max_score=1.0,
            response_outcome="correct" if correct else "incorrect",
            outcome_policy_version="binary-policy-1",
            source_audit_id=f"audit_{learner_id}_{index}",
            source_audit_version=1,
            occurred_at=NOW + timedelta(minutes=3, seconds=index),
        )
        for index in range(3)
    ]


def test_parameter_versions_are_append_only(tmp_path: Path) -> None:
    """Catch overwriting one parameter identity or course/version pair."""

    repository = _repository(tmp_path / "append-only.sqlite3")
    shadow = _shadow_parameter_set()

    assert repository.insert_or_get_parameter_set(
        shadow,
        course_id="course_1",
    ) == shadow
    with pytest.raises(RuntimeError, match="parameter-set conflict"):
        repository.insert_or_get_parameter_set(
            shadow.model_copy(update={"sample_size": 999}),
            course_id="course_1",
        )
    with pytest.raises(RuntimeError, match="parameter-set conflict"):
        repository.insert_or_get_parameter_set(
            shadow.model_copy(update={"parameter_set_id": "different_id"}),
            course_id="course_1",
        )


def test_calibration_is_persisted_with_its_course_scope(tmp_path: Path) -> None:
    """Catch returning a shadow run without making it restart-recoverable."""

    run = _shadow_run()

    class StaticCalibrator:
        def fit(self, observations, requested_at):
            return run

    repository = _repository(tmp_path / "calibration.sqlite3")
    service = _service(repository, calibrator=StaticCalibrator())
    response = _responses(learner_id="learner_1", correct=True)[0]
    batch = LearningObservationBatch(
        batch_id="batch_task9",
        learner_id=response.learner_id,
        observations=[response],
        watermark="wm_task9",
        created_at=NOW,
    )

    assert service.calibrate_irt(batch, NOW) == run
    assert repository.get_calibration_run(run.run_id) == run
    assert repository.get_calibration_run_course_id(run.run_id) == "course_1"
    assert repository.get_parameter_set(run.parameter_set.parameter_set_id) == (
        run.parameter_set
    )


def test_approval_creates_restart_recoverable_immutable_version(
    tmp_path: Path,
) -> None:
    """Catch mutating the shadow version or keeping approval only in memory."""

    database_path = tmp_path / "approval.sqlite3"
    service = _service(_repository(database_path))
    run = _shadow_run()
    service.store_calibration_result(run, course_id="course_1")
    decision = _decision(run, decision="approve")

    approved = service.apply_calibration_review(
        run.run_id,
        _quality(run),
        decision,
    )

    assert approved.status == "approved"
    assert approved.parameter_set_id != run.parameter_set.parameter_set_id
    assert approved.version != run.parameter_set.version
    assert approved.item_parameters == run.parameter_set.item_parameters
    assert service.apply_calibration_review(
        run.run_id,
        _quality(run),
        decision,
    ) == approved
    restarted = _service(_repository(database_path))
    assert restarted.activate_parameter_set(approved.parameter_set_id) == approved
    assert restarted.get_active_parameter_set(approved.parameter_set_id) == approved
    assert restarted.get_parameter_set(run.parameter_set.parameter_set_id) == (
        run.parameter_set
    )


def test_review_state_machine_rejects_invalid_transitions(tmp_path: Path) -> None:
    """Catch approval without ready evidence and activation of rejected models."""

    service = _service(_repository(tmp_path / "review-state.sqlite3"))
    run = _shadow_run()
    service.store_calibration_result(run, course_id="course_1")

    with pytest.raises(DomainError, match="QUALITY_REPORT_NOT_READY"):
        service.apply_calibration_review(
            run.run_id,
            _quality(run, status="insufficient_data"),
            _decision(run, decision="approve"),
        )

    rejected = service.apply_calibration_review(
        run.run_id,
        _quality(run, status="insufficient_data"),
        _decision(run, decision="reject"),
    )
    assert rejected.status == "rejected"
    with pytest.raises(DomainError, match="approved"):
        service.activate_parameter_set(rejected.parameter_set_id)
    with pytest.raises((DomainError, RuntimeError), match="review conflict"):
        service.apply_calibration_review(
            run.run_id,
            _quality(run),
            _decision(run, decision="approve", decision_id="later_approval"),
        )


def test_defer_keeps_shadow_parameters_inactive(tmp_path: Path) -> None:
    """Catch treating a deferred review as approval."""

    service = _service(_repository(tmp_path / "defer.sqlite3"))
    run = _shadow_run()
    service.store_calibration_result(run, course_id="course_1")

    deferred = service.apply_calibration_review(
        run.run_id,
        _quality(run),
        _decision(run, decision="defer"),
    )

    assert deferred == run.parameter_set
    with pytest.raises(DomainError, match="approved"):
        service.activate_parameter_set(deferred.parameter_set_id)


def test_eap_estimates_are_finite_persisted_and_approved_only(
    tmp_path: Path,
) -> None:
    """Catch unbounded extreme scores or bypassing the approval gate."""

    database_path = tmp_path / "ability.sqlite3"
    service = _service(_repository(database_path))
    run = _shadow_run()
    service.store_calibration_result(run, course_id="course_1")
    with pytest.raises(DomainError, match="approved"):
        service.estimate_ability(
            run.parameter_set.parameter_set_id,
            _responses(learner_id="shadow_learner", correct=True),
            course_id="course_1",
        )
    approved = service.apply_calibration_review(
        run.run_id,
        _quality(run),
        _decision(run, decision="approve"),
    )

    all_correct = service.estimate_ability(
        approved.parameter_set_id,
        _responses(learner_id="correct_learner", correct=True),
        course_id="course_1",
    )
    all_incorrect = service.estimate_ability(
        approved.parameter_set_id,
        _responses(learner_id="incorrect_learner", correct=False),
        course_id="course_1",
    )

    assert all_correct.status == all_incorrect.status == "estimated"
    assert all_correct.theta > all_incorrect.theta
    assert all(
        math.isfinite(value)
        for value in (
            all_correct.theta,
            all_correct.standard_error,
            all_incorrect.theta,
            all_incorrect.standard_error,
        )
    )
    restarted = _service(_repository(database_path))
    assert restarted.get_ability_estimate(all_correct.estimate_id) == all_correct
    assert restarted.get_ability_estimate(all_incorrect.estimate_id) == all_incorrect
