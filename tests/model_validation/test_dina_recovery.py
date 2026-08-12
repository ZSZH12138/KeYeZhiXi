"""Deterministic recovery test for the production DINA implementation."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from course_insight.contracts.knowledge import QMatrixEntry
from course_insight.contracts.learning_models import (
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.modules.m5_learner_class_state.dina import DinaEngine


SEED = 20260812
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)


def _auc(truth: list[bool], scores: list[float]) -> float:
    positive = [score for value, score in zip(truth, scores, strict=True) if value]
    negative = [score for value, score in zip(truth, scores, strict=True) if not value]
    wins = sum(
        1.0 if positive_score > negative_score else 0.5
        for positive_score in positive
        for negative_score in negative
        if positive_score >= negative_score
    )
    return wins / (len(positive) * len(negative))


def _synthetic_dina_case() -> tuple[
    list[LearningObservationBatch],
    list[QMatrixEntry],
    dict[tuple[str, str], tuple[float, float]],
    dict[str, list[bool]],
]:
    generator = random.Random(SEED)
    concept_ids = [f"concept_{index}" for index in range(4)]
    requirements = [
        (concept_ids[item_index % 4],)
        if item_index < 12
        else (
            concept_ids[item_index % 4],
            concept_ids[(item_index + 1) % 4],
        )
        for item_index in range(20)
    ]
    true_parameters = {
        (f"item_{item_index}", "1.0.0"): (
            0.06 + 0.015 * (item_index % 5),
            0.10 + 0.02 * (item_index % 4),
        )
        for item_index in range(20)
    }
    q_matrix = [
        QMatrixEntry(
            item_id=f"item_{item_index}",
            item_version="1.0.0",
            concept_id=concept_id,
            weight=1.0,
        )
        for item_index, item_requirements in enumerate(requirements)
        for concept_id in item_requirements
    ]
    truths = {concept_id: [] for concept_id in concept_ids}
    cohort: list[LearningObservationBatch] = []
    priors = (0.35, 0.45, 0.55, 0.65)
    for learner_index in range(500):
        learner_id = f"learner_{learner_index:04d}"
        profile = {
            concept_id: generator.random() < priors[concept_index]
            for concept_index, concept_id in enumerate(concept_ids)
        }
        for concept_id in concept_ids:
            truths[concept_id].append(profile[concept_id])
        observations: list[LearningObservation] = []
        for item_index, item_requirements in enumerate(requirements):
            slip, guess = true_parameters[(f"item_{item_index}", "1.0.0")]
            capable = all(profile[concept_id] for concept_id in item_requirements)
            probability = 1.0 - slip if capable else guess
            correct = generator.random() < probability
            observations.append(
                LearningObservation(
                    observation_id=f"obs_{learner_index}_{item_index}",
                    learner_id=learner_id,
                    course_id="course_1",
                    class_id="class_1",
                    attempt_id=f"attempt_{learner_index}",
                    item_id=f"item_{item_index}",
                    item_version="1.0.0",
                    concept_ids=list(item_requirements),
                    score=1.0 if correct else 0.0,
                    max_score=1.0,
                    response_outcome="correct" if correct else "incorrect",
                    outcome_policy_version="binary-policy-1",
                    source_audit_id=f"audit_{learner_index}_{item_index}",
                    source_audit_version=1,
                    occurred_at=NOW + timedelta(seconds=item_index),
                )
            )
        cohort.append(
            LearningObservationBatch(
                batch_id=f"batch_{learner_index}",
                learner_id=learner_id,
                observations=observations,
                watermark=f"watermark_{learner_index}",
                created_at=NOW + timedelta(minutes=learner_index),
            )
        )
    return cohort, q_matrix, true_parameters, truths


def test_dina_recovers_mastery_and_item_parameters_from_fixed_data() -> None:
    """Catch a formally valid DINA implementation that cannot recover signal."""

    cohort, q_matrix, true_parameters, truths = _synthetic_dina_case()
    engine = DinaEngine(max_iterations=120)

    model = engine.fit(cohort, q_matrix)
    diagnoses = [engine.infer(model, batch) for batch in cohort]

    auc_by_concept = {
        concept_id: _auc(
            truths[concept_id],
            [result.concept_mastery[concept_id] for result in diagnoses],
        )
        for concept_id in truths
    }
    parameter_errors = [
        abs(item.slip - true_parameters[(item.item_id, item.item_version)][0])
        + abs(item.guess - true_parameters[(item.item_id, item.item_version)][1])
        for item in model.item_parameters
    ]

    assert min(auc_by_concept.values()) >= 0.85
    assert sum(parameter_errors) / (2 * len(parameter_errors)) <= 0.08
    assert model.inference_mode == "exact"
    assert model.learner_count == 500
