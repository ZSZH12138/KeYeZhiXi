"""Integration tests for the persisted IRT approval and ability workflow."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionResult,
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


def test_model_runtime_rejects_missing_scope_identity_and_invalid_ability(
    tmp_path: Path,
) -> None:
    """Public lifecycle errors stay stable instead of leaking repository details."""

    service = _service(_repository(tmp_path / "runtime-errors.sqlite3"))
    run = _shadow_run()

    with pytest.raises(DomainError) as missing_run:
        service.apply_calibration_review(
            run.run_id,
            _quality(run),
            _decision(run, decision="approve"),
        )
    assert missing_run.value.code == "CALIBRATION_RUN_NOT_FOUND"
    with pytest.raises(DomainError) as missing_parameter:
        service.get_parameter_set("missing_parameter")
    assert missing_parameter.value.code == "IRT_PARAMETER_SET_NOT_FOUND"
    with pytest.raises(DomainError) as missing_ability:
        service.get_ability_estimate("missing_ability")
    assert missing_ability.value.code == "ABILITY_ESTIMATE_NOT_FOUND"

    service.store_calibration_result(run, course_id="course_1")
    mismatched_decision = _decision(run, decision="reject").model_copy(
        update={"calibration_run_id": "other_run"}
    )
    with pytest.raises(DomainError) as identity:
        service.apply_calibration_review(
            run.run_id,
            _quality(run),
            mismatched_decision,
        )
    assert identity.value.code == "CALIBRATION_REVIEW_IDENTITY_MISMATCH"

    approved = service.apply_calibration_review(
        run.run_id,
        _quality(run),
        _decision(run, decision="approve"),
    )
    with pytest.raises(DomainError) as scope:
        service.estimate_ability(
            approved.parameter_set_id,
            _responses(learner_id="learner_scope", correct=True),
            course_id="course_2",
        )
    assert scope.value.code == "IRT_PARAMETER_SCOPE_MISMATCH"
    with pytest.raises(DomainError) as invalid:
        service.estimate_ability(
            approved.parameter_set_id,
            [],
            course_id="course_1",
        )
    assert invalid.value.code == "ABILITY_ESTIMATION_INVALID"


def test_calibration_persistence_validates_single_course_and_repository_result(
    tmp_path: Path,
) -> None:
    """Calibration persistence must not mix courses or accept altered writes."""

    service = _service(_repository(tmp_path / "calibration-errors.sqlite3"))
    run = _shadow_run()
    mixed = [
        _responses(learner_id="learner_1", correct=True)[0],
        _responses(learner_id="learner_2", correct=False)[0].model_copy(
            update={"course_id": "course_2"}
        ),
    ]
    with pytest.raises(DomainError) as scope:
        service._persist_calibration_if_supported(run, mixed)
    assert scope.value.code == "CALIBRATION_SCOPE_MISMATCH"

    class NoCalibrationPersistence:
        pass

    no_persistence = _service(NoCalibrationPersistence())
    assert no_persistence._persist_calibration_if_supported(run, mixed[:1]) == run
    with pytest.raises(RuntimeError, match="does not support"):
        no_persistence.store_calibration_result(run, course_id="course_1")

    class AlteringRepository(NoCalibrationPersistence):
        def insert_or_get_calibration_run(self, result, *, course_id):
            return result.model_copy(update={"run_id": "altered_run"})

    with pytest.raises(RuntimeError, match="calibration-run conflict"):
        _service(AlteringRepository()).store_calibration_result(
            run,
            course_id="course_1",
        )


def test_sqlite_model_runtime_guards_every_append_only_artifact(
    tmp_path: Path,
) -> None:
    """Calibration, ability and adaptive history reject altered retries."""

    from course_insight.infrastructure.sqlite import m8_model_runtime

    repository = _repository(tmp_path / "runtime-persistence-guards.sqlite3")
    run = _shadow_run()
    repository.insert_or_get_calibration_run(run, course_id="course_1")
    assert repository.list_parameter_sets(course_id="course_1") == [
        run.parameter_set
    ]
    with pytest.raises(ValueError, match="scope"):
        repository.list_parameter_sets(course_id="   ")
    with pytest.raises(RuntimeError, match="calibration-run conflict"):
        repository.insert_or_get_calibration_run(
            run.model_copy(update={"metrics": {"log_likelihood": -999.0}}),
            course_id="course_1",
        )

    approved = run.parameter_set.model_copy(
        update={
            "parameter_set_id": "irt_approved_guards",
            "version": "irt-task9-v1-approved-guards",
            "status": "approved",
            "created_at": NOW + timedelta(minutes=2),
        }
    )
    decision = _decision(run, decision="approve", decision_id="decision_guards")
    with pytest.raises(RuntimeError, match="review conflict"):
        repository.insert_or_get_calibration_review(
            decision,
            approved,
            course_id="course_2",
        )
    repository.insert_or_get_parameter_set(approved, course_id="course_1")

    invalid_estimate = AbilityEstimate(
        estimate_id="ability_shadow",
        learner_id="learner_1",
        parameter_set_id=run.parameter_set.parameter_set_id,
        theta=0.0,
        standard_error=1.0,
        status="estimated",
        estimated_at=NOW + timedelta(minutes=3),
    )
    with pytest.raises(RuntimeError, match="ability-estimate conflict"):
        repository.insert_or_get_ability_estimate(
            invalid_estimate,
            course_id="course_1",
        )

    estimate = invalid_estimate.model_copy(
        update={
            "estimate_id": "ability_approved_guards",
            "parameter_set_id": approved.parameter_set_id,
        }
    )
    assert repository.insert_or_get_ability_estimate(
        estimate,
        course_id="course_1",
    ) == estimate
    with pytest.raises(RuntimeError, match="ability-estimate conflict"):
        repository.insert_or_get_ability_estimate(
            estimate.model_copy(update={"theta": 0.5}),
            course_id="course_1",
        )

    empty_selection = AdaptiveSelectionResult(
        selection_id="selection_empty_guard",
        policy_id="policy_guard",
        learner_id="learner_1",
        item_ids=[],
        ability_estimate=None,
        status="empty",
        failure_code=None,
        selected_at=NOW + timedelta(minutes=4),
    )
    with pytest.raises(ValueError, match="unconfigured"):
        repository.insert_or_get_adaptive_selection(
            empty_selection,
            course_id="course_1",
        )

    selection = empty_selection.model_copy(
        update={
            "selection_id": "selection_guard",
            "item_ids": ["item_1"],
            "ability_estimate": estimate,
            "status": "selected",
        }
    )
    assert repository.insert_or_get_adaptive_selection(
        selection,
        course_id="course_1",
    ) == selection
    with pytest.raises(RuntimeError, match="adaptive-selection conflict"):
        repository.insert_or_get_adaptive_selection(
            selection.model_copy(update={"item_ids": ["item_2"]}),
            course_id="course_1",
        )
    with pytest.raises(ValueError, match="unsupported"):
        m8_model_runtime._scope_value(
            tmp_path / "runtime-persistence-guards.sqlite3",
            "bad_table",
            "bad_id",
            "value",
        )
