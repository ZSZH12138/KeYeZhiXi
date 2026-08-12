"""Unit tests for real two-parameter logistic IRT calibration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from course_insight.contracts.learning_models import (
    IRTItemParameters,
    IRTParameterSet,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService


NOW = datetime(2026, 8, 12, 13, 0, tzinfo=UTC)


def _calibrator(**overrides):
    from course_insight.modules.m8_assessment_scoring.irt_2pl import (
        TwoPLCalibrator,
    )

    return TwoPLCalibrator(**overrides)


def _observation(
    learner_index: int,
    item_index: int,
    correct: bool,
) -> LearningObservation:
    return LearningObservation(
        observation_id=f"obs_{learner_index}_{item_index}",
        learner_id=f"learner_{learner_index}",
        course_id="course_1",
        class_id="class_1",
        attempt_id=f"attempt_{learner_index}",
        item_id=f"item_{item_index}",
        item_version="1.0.0",
        concept_ids=["concept_1"],
        score=1.0 if correct else 0.0,
        max_score=1.0,
        response_outcome="correct" if correct else "incorrect",
        outcome_policy_version="binary-policy-1",
        source_audit_id=f"audit_{learner_index}_{item_index}",
        source_audit_version=1,
        occurred_at=NOW + timedelta(seconds=item_index),
    )


def test_higher_ability_increases_success_probability() -> None:
    """Catch reversing the theta-minus-difficulty term."""

    calibrator = _calibrator()

    assert calibrator.probability(
        theta=1.0,
        discrimination=1.2,
        difficulty=0.0,
    ) > calibrator.probability(
        theta=-1.0,
        discrimination=1.2,
        difficulty=0.0,
    )


def test_higher_discrimination_increases_information_near_difficulty() -> None:
    """Catch omitting squared discrimination from Fisher information."""

    calibrator = _calibrator()

    assert calibrator.information(
        theta=0.0,
        discrimination=2.0,
        difficulty=0.0,
    ) > calibrator.information(
        theta=0.0,
        discrimination=0.5,
        difficulty=0.0,
    )


def test_single_response_returns_explicit_insufficient_data_failure() -> None:
    """Catch claiming convergence or parameters from one response."""

    result = _calibrator().fit([_observation(0, 0, True)], NOW)

    assert result.status == "failed"
    assert result.failure_code == "INSUFFICIENT_CALIBRATION_DATA"
    assert not result.converged
    assert result.parameter_set.status == "empty"
    assert result.parameter_set.item_parameters == []


def test_eap_ability_uses_only_approved_parameter_sets() -> None:
    """Catch using unreviewed shadow parameters for learner decisions."""

    parameters = IRTParameterSet(
        parameter_set_id="irt_approved_1",
        model_type="2PL",
        version="irt-v1",
        item_parameters=[
            IRTItemParameters(
                item_id="item_0",
                item_version="1.0.0",
                discrimination=1.5,
                difficulty=0.0,
                guessing=0.0,
                sample_size=200,
            )
        ],
        sample_size=200,
        status="approved",
        created_at=NOW,
    )

    correct = _calibrator().estimate_ability(
        parameters,
        [_observation(0, 0, True)],
    )
    incorrect = _calibrator().estimate_ability(
        parameters,
        [_observation(0, 0, False)],
    )

    assert correct.status == "estimated"
    assert incorrect.status == "estimated"
    assert correct.theta > incorrect.theta
    with pytest.raises(ValueError, match="approved"):
        _calibrator().estimate_ability(
            parameters.model_copy(update={"status": "shadow"}),
            [_observation(0, 0, True)],
        )
    with pytest.raises(ValueError, match="2PL"):
        _calibrator().estimate_ability(
            parameters.model_copy(update={"model_type": "1PL"}),
            [_observation(0, 0, True)],
        )


def test_service_flattens_multiple_single_learner_batches_for_calibration() -> None:
    """Keep the learner-scoped batch contract while enabling cohort calibration."""

    class _CalibrationSpy:
        def __init__(self) -> None:
            self.observation_ids: list[str] = []

        def fit(self, observations, requested_at):
            self.observation_ids = [item.observation_id for item in observations]
            return _calibrator().fit(observations, requested_at)

    spy = _CalibrationSpy()
    service = M8AssessmentService(
        repository=object(),
        rule_scorer=object(),
        parameter_item_generator=object(),
        irt_calibrator=spy,
    )
    batches = [
        LearningObservationBatch(
            batch_id="batch_0",
            learner_id="learner_0",
            observations=[_observation(0, 0, True)],
            watermark="watermark_0",
            created_at=NOW,
        ),
        LearningObservationBatch(
            batch_id="batch_1",
            learner_id="learner_1",
            observations=[_observation(1, 0, False)],
            watermark="watermark_1",
            created_at=NOW,
        ),
    ]

    result = service.calibrate_irt(batches, NOW)

    assert result.failure_code == "INSUFFICIENT_CALIBRATION_DATA"
    assert spy.observation_ids == ["obs_0_0", "obs_1_0"]


def test_identical_irt_request_replays_an_equal_result() -> None:
    """Catch nondeterministic identities for an exact calibration retry."""

    observations = [
        _observation(learner, item, (learner + item) % 2 == 0)
        for learner in range(8)
        for item in range(3)
    ]
    calibrator = _calibrator(
        min_students=8,
        min_responses_per_item=8,
        min_items=3,
        max_iterations=100,
    )

    first = calibrator.fit(observations, NOW)
    replay = calibrator.fit(list(reversed(observations)), NOW)

    assert first.status == "shadow"
    assert replay == first


def test_later_irt_run_reuses_parameters_without_reusing_run_identity() -> None:
    """Catch one run ID referring to timestamp-dependent result content."""

    observations = [
        _observation(learner, item, (learner + item) % 2 == 0)
        for learner in range(8)
        for item in range(3)
    ]
    calibrator = _calibrator(
        min_students=8,
        min_responses_per_item=8,
        min_items=3,
        max_iterations=100,
    )

    first = calibrator.fit(observations, NOW)
    later = calibrator.fit(observations, NOW + timedelta(minutes=5))

    assert first.status == "shadow"
    assert later.parameter_set == first.parameter_set
    assert later.run_id != first.run_id
    assert later.generated_at == NOW + timedelta(minutes=5)


def test_equivalent_irt_request_timezones_have_one_immutable_payload() -> None:
    """Catch one run ID serializing two offsets for the same instant."""

    observations = [
        _observation(learner, item, (learner + item) % 2 == 0)
        for learner in range(8)
        for item in range(3)
    ]
    calibrator = _calibrator(
        min_students=8,
        min_responses_per_item=8,
        min_items=3,
        max_iterations=100,
    )

    utc_result = calibrator.fit(observations, NOW)
    offset_result = calibrator.fit(
        observations,
        NOW.astimezone(timezone(timedelta(hours=8))),
    )

    assert offset_result.run_id == utc_result.run_id
    assert offset_result.content_checksum() == utc_result.content_checksum()
