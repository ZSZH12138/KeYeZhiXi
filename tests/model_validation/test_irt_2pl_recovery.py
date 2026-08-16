"""Fixed-seed recovery acceptance test for 2PL marginal calibration."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from scipy.special import expit

from course_insight.contracts.learning_models import LearningObservation
from course_insight.modules.m8_assessment_scoring.irt_2pl import TwoPLCalibrator


SEED = 20260812
REQUESTED_AT = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
STUDENT_COUNT = 1_000
ITEM_COUNT = 30


def _synthetic_observations() -> tuple[
    list[LearningObservation],
    dict[str, tuple[float, float]],
]:
    rng = np.random.default_rng(SEED)
    abilities = rng.normal(0.0, 1.0, size=STUDENT_COUNT)
    discriminations = np.linspace(0.55, 1.95, ITEM_COUNT)
    difficulties = np.linspace(-1.80, 1.80, ITEM_COUNT)
    rng.shuffle(discriminations)
    rng.shuffle(difficulties)
    probabilities = expit(
        discriminations[None, :]
        * (abilities[:, None] - difficulties[None, :])
    )
    outcomes = rng.random((STUDENT_COUNT, ITEM_COUNT)) < probabilities
    observations = [
        LearningObservation(
            observation_id=f"obs_{student_index}_{item_index}",
            learner_id=f"learner_{student_index}",
            course_id="course_irt_validation",
            class_id="class_irt_validation",
            attempt_id=f"attempt_{student_index}",
            item_id=f"item_{item_index}",
            item_version="1.0.0",
            concept_ids=[f"concept_{item_index % 5}"],
            score=1.0 if outcomes[student_index, item_index] else 0.0,
            max_score=1.0,
            response_outcome=(
                "correct" if outcomes[student_index, item_index] else "incorrect"
            ),
            outcome_policy_version="binary-policy-1",
            source_audit_id=f"audit_{student_index}_{item_index}",
            source_audit_version=1,
            occurred_at=REQUESTED_AT,
        )
        for student_index in range(STUDENT_COUNT)
        for item_index in range(ITEM_COUNT)
    ]
    truth = {
        f"item_{item_index}": (
            float(discriminations[item_index]),
            float(difficulties[item_index]),
        )
        for item_index in range(ITEM_COUNT)
    }
    return observations, truth


def test_fixed_seed_2pl_recovers_item_parameters() -> None:
    """Require useful recovery, real convergence evidence, and shadow status."""

    observations, truth = _synthetic_observations()

    result = TwoPLCalibrator().fit(observations, REQUESTED_AT)

    assert result.status == "shadow"
    assert result.converged
    assert result.parameter_set.status == "shadow"
    assert result.parameter_set.sample_size == STUDENT_COUNT
    assert len(result.parameter_set.item_parameters) == ITEM_COUNT
    assert {
        "log_likelihood",
        "aic",
        "bic",
        "iteration_count",
    } <= result.metrics.keys()

    ordered = sorted(
        result.parameter_set.item_parameters,
        key=lambda item: int(item.item_id.removeprefix("item_")),
    )
    estimated_discrimination = np.array(
        [item.discrimination for item in ordered]
    )
    estimated_difficulty = np.array([item.difficulty for item in ordered])
    true_discrimination = np.array(
        [truth[item.item_id][0] for item in ordered]
    )
    true_difficulty = np.array([truth[item.item_id][1] for item in ordered])

    discrimination_correlation = float(
        np.corrcoef(estimated_discrimination, true_discrimination)[0, 1]
    )
    difficulty_correlation = float(
        np.corrcoef(estimated_difficulty, true_difficulty)[0, 1]
    )
    discrimination_rmse = float(
        np.sqrt(np.mean((estimated_discrimination - true_discrimination) ** 2))
    )
    difficulty_rmse = float(
        np.sqrt(np.mean((estimated_difficulty - true_difficulty) ** 2))
    )

    assert difficulty_correlation >= 0.90
    assert difficulty_rmse <= 0.35
    assert discrimination_correlation >= 0.85
    assert discrimination_rmse <= 0.30
