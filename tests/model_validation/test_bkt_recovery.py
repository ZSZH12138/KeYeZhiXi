"""Fixed-seed recovery and prediction checks for four-parameter BKT."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

from course_insight.contracts.learning_models import (
    ConceptResponse,
    ConceptResponseSequence,
)
from course_insight.modules.m5_learner_class_state.bkt import BktEngine


SEED = 20260812
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _synthetic_bkt_case() -> tuple[
    list[ConceptResponseSequence],
    dict[str, tuple[float, float, float, float]],
]:
    generator = random.Random(SEED)
    true_parameters = {
        "concept_0": (0.20, 0.10, 0.15, 0.10),
        "concept_1": (0.30, 0.15, 0.20, 0.08),
        "concept_2": (0.40, 0.20, 0.10, 0.12),
        "concept_3": (0.50, 0.08, 0.18, 0.15),
    }
    sequences: list[ConceptResponseSequence] = []
    for concept_id, (prior, learn, guess, slip) in true_parameters.items():
        for learner_index in range(300):
            learner_id = f"learner_{learner_index:03d}"
            mastered = generator.random() < prior
            responses: list[ConceptResponse] = []
            for response_index in range(30):
                correct_probability = 1.0 - slip if mastered else guess
                correct = generator.random() < correct_probability
                responses.append(
                    ConceptResponse(
                        observation_id=(
                            f"obs_{concept_id}_{learner_index}_{response_index}"
                        ),
                        learner_id=learner_id,
                        course_id="course_1",
                        class_id="class_1",
                        attempt_id=f"attempt_{concept_id}_{learner_index}",
                        concept_id=concept_id,
                        is_correct=correct,
                        source_audit_id=(
                            f"audit_{concept_id}_{learner_index}_{response_index}"
                        ),
                        source_audit_version=1,
                        occurred_at=NOW + timedelta(minutes=response_index),
                    )
                )
                if not mastered and generator.random() < learn:
                    mastered = True
            sequences.append(
                ConceptResponseSequence(
                    sequence_id=f"sequence_{concept_id}_{learner_index}",
                    learner_id=learner_id,
                    course_id="course_1",
                    class_id="class_1",
                    concept_id=concept_id,
                    responses=responses,
                    watermark=f"watermark_{concept_id}_{learner_index}",
                    created_at=NOW + timedelta(hours=1),
                )
            )
    return sequences, true_parameters


def _prediction_log_loss(
    sequences: list[ConceptResponseSequence],
    fitted_parameters: dict[str, tuple[float, float, float, float]],
) -> float:
    engine = BktEngine()
    losses: list[float] = []
    for sequence in sequences:
        prior, learn, guess, slip = fitted_parameters[sequence.concept_id]
        mastery = prior
        for response in sequence.responses:
            correct_probability = mastery * (1.0 - slip) + (1.0 - mastery) * guess
            correct_probability = min(1.0 - 1e-12, max(1e-12, correct_probability))
            losses.append(
                -math.log(
                    correct_probability
                    if response.is_correct
                    else 1.0 - correct_probability
                )
            )
            posterior = engine.observation_update(
                prior=mastery,
                correct=response.is_correct,
                guess=guess,
                slip=slip,
            )
            mastery = posterior + (1.0 - posterior) * learn
    return math.fsum(losses) / len(losses)


def test_bkt_recovers_parameters_and_beats_global_correct_rate() -> None:
    """Catch a BKT fitter that cannot recover or predict temporal signal."""

    sequences, true_parameters = _synthetic_bkt_case()
    model = BktEngine(max_iterations=120).fit(sequences)
    fitted = {
        item.concept_id: (item.prior, item.learn, item.guess, item.slip)
        for item in model.concept_parameters
    }
    absolute_errors = [
        abs(estimated - truth)
        for concept_id, true_values in true_parameters.items()
        for estimated, truth in zip(fitted[concept_id], true_values, strict=True)
    ]
    outcomes = [
        response.is_correct
        for sequence in sequences
        for response in sequence.responses
    ]
    correct_rate = sum(outcomes) / len(outcomes)
    baseline_loss = -(
        correct_rate * math.log(correct_rate)
        + (1.0 - correct_rate) * math.log(1.0 - correct_rate)
    )
    bkt_loss = _prediction_log_loss(sequences, fitted)

    assert math.fsum(absolute_errors) / len(absolute_errors) <= 0.08
    assert bkt_loss <= baseline_loss * 0.90
    assert model.learner_count == 300
    assert model.observation_count == 36_000
