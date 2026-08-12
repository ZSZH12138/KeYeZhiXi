"""Unit tests for the DINA cognitive-diagnosis engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import QMatrixEntry
from course_insight.contracts.learning_models import (
    LearningObservation,
    LearningObservationBatch,
)


NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def _engine():
    from course_insight.modules.m5_learner_class_state.dina import DinaEngine

    return DinaEngine()


def _batch(
    learner_id: str,
    outcomes: tuple[bool, bool],
    *,
    offset: int = 0,
) -> LearningObservationBatch:
    observations = []
    for index, (item_id, concept_id, correct) in enumerate(
        zip(
            ("item_1", "item_2"),
            ("concept_1", "concept_2"),
            outcomes,
            strict=True,
        )
    ):
        observations.append(
            LearningObservation(
                observation_id=f"obs_{learner_id}_{item_id}_{offset}",
                learner_id=learner_id,
                course_id="course_1",
                class_id="class_1",
                attempt_id=f"attempt_{learner_id}_{offset}",
                item_id=item_id,
                item_version="1.0.0",
                concept_ids=[concept_id],
                score=1.0 if correct else 0.0,
                max_score=1.0,
                response_outcome="correct" if correct else "incorrect",
                outcome_policy_version="binary-policy-1",
                source_audit_id=f"audit_{learner_id}_{item_id}_{offset}",
                source_audit_version=1,
                occurred_at=NOW + timedelta(minutes=index + offset),
            )
        )
    return LearningObservationBatch(
        batch_id=f"batch_{learner_id}_{offset}",
        learner_id=learner_id,
        observations=observations,
        watermark=f"watermark_{learner_id}_{offset}",
        created_at=NOW + timedelta(minutes=10 + offset),
    )


def _q_matrix() -> list[QMatrixEntry]:
    return [
        QMatrixEntry(
            item_id="item_1",
            item_version="1.0.0",
            concept_id="concept_1",
            weight=1.0,
        ),
        QMatrixEntry(
            item_id="item_2",
            item_version="1.0.0",
            concept_id="concept_2",
            weight=1.0,
        ),
    ]


def _training_cohort() -> list[LearningObservationBatch]:
    return [
        _batch("learner_1", (True, True)),
        _batch("learner_2", (True, False)),
        _batch("learner_3", (False, True)),
        _batch("learner_4", (False, False)),
    ]


def _high_dimensional_case() -> tuple[
    list[LearningObservationBatch],
    list[QMatrixEntry],
]:
    q_matrix: list[QMatrixEntry] = []
    for item_index in range(20):
        for concept_index in (item_index, (item_index + 1) % 20):
            q_matrix.append(
                QMatrixEntry(
                    item_id=f"connected_item_{item_index}",
                    item_version="1.0.0",
                    concept_id=f"connected_concept_{concept_index}",
                    weight=1.0,
                )
            )
    cohort: list[LearningObservationBatch] = []
    for learner_index in range(4):
        learner_id = f"connected_learner_{learner_index}"
        observations = [
            LearningObservation(
                observation_id=f"connected_obs_{learner_index}_{item_index}",
                learner_id=learner_id,
                course_id="course_1",
                class_id="class_1",
                attempt_id=f"connected_attempt_{learner_index}",
                item_id=f"connected_item_{item_index}",
                item_version="1.0.0",
                concept_ids=[
                    f"connected_concept_{item_index}",
                    f"connected_concept_{(item_index + 1) % 20}",
                ],
                score=float((learner_index + item_index) % 2 == 0),
                max_score=1.0,
                response_outcome=(
                    "correct"
                    if (learner_index + item_index) % 2 == 0
                    else "incorrect"
                ),
                outcome_policy_version="binary-policy-1",
                source_audit_id=f"connected_audit_{learner_index}_{item_index}",
                source_audit_version=1,
                occurred_at=NOW + timedelta(minutes=item_index),
            )
            for item_index in range(20)
        ]
        cohort.append(
            LearningObservationBatch(
                batch_id=f"connected_batch_{learner_index}",
                learner_id=learner_id,
                observations=observations,
                watermark=f"connected_watermark_{learner_index}",
                created_at=NOW + timedelta(hours=learner_index),
            )
        )
    return cohort, q_matrix


def test_mastered_profile_uses_one_minus_slip() -> None:
    """Catch treating a capable learner's success probability as slip."""

    assert _engine().response_probability(
        capable=True,
        slip=0.1,
        guess=0.2,
    ) == pytest.approx(0.9)


def test_unmastered_profile_uses_guess_probability() -> None:
    """Catch granting an incapable learner the mastered response rate."""

    assert _engine().response_probability(
        capable=False,
        slip=0.1,
        guess=0.2,
    ) == pytest.approx(0.2)


def test_q_matrix_requires_every_attribute_for_the_dina_and_gate() -> None:
    """Catch changing DINA's conjunctive Q-matrix rule into an OR rule."""

    engine = _engine()

    assert engine.is_capable(
        profile={"concept_1": True, "concept_2": True},
        required={"concept_1", "concept_2"},
    )
    assert not engine.is_capable(
        profile={"concept_1": True, "concept_2": False},
        required={"concept_1", "concept_2"},
    )


def test_fit_rejects_cohorts_below_the_governed_data_threshold() -> None:
    """Catch fitting and publishing parameters from insufficient evidence."""

    from course_insight.modules.m5_learner_class_state.dina import DinaEngine

    engine = DinaEngine(min_students=4, min_responses_per_item=2)

    with pytest.raises(DomainError) as captured:
        engine.fit(_training_cohort()[:3], _q_matrix())

    assert captured.value.code == "INSUFFICIENT_MODEL_DATA"


def test_fit_is_deterministic_and_keeps_parameters_in_dina_bounds() -> None:
    """Catch unstable model identities or degenerate slip/guess estimates."""

    from course_insight.modules.m5_learner_class_state.dina import DinaEngine

    engine = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=30,
    )

    first = engine.fit(_training_cohort(), _q_matrix())
    second = engine.fit(list(reversed(_training_cohort())), _q_matrix())

    assert first == second
    assert first.inference_mode == "exact"
    assert first.learner_count == 4
    assert first.observation_count == 8
    assert all(
        0.01 <= item.slip <= 0.40 and 0.01 <= item.guess <= 0.40
        for item in first.item_parameters
    )


def test_profile_posterior_is_normalized_and_drives_mastery_direction() -> None:
    """Catch invalid posterior mass or diagnosis unrelated to responses."""

    from course_insight.contracts.learning_models import (
        DinaItemParameters,
        DinaModelArtifact,
    )
    from course_insight.modules.m5_learner_class_state.dina import DinaEngine

    model = DinaModelArtifact(
        model_id="dina_model_1",
        course_id="course_1",
        class_id="class_1",
        model_version="dina-v1",
        concept_ids=["concept_1", "concept_2"],
        item_parameters=[
            DinaItemParameters(
                item_id="item_1",
                item_version="1.0.0",
                concept_ids=["concept_1"],
                slip=0.1,
                guess=0.2,
                sample_size=100,
            ),
            DinaItemParameters(
                item_id="item_2",
                item_version="1.0.0",
                concept_ids=["concept_2"],
                slip=0.1,
                guess=0.2,
                sample_size=100,
            ),
        ],
        attribute_priors={"concept_1": 0.5, "concept_2": 0.5},
        learner_count=100,
        observation_count=200,
        inference_mode="exact",
        log_likelihood=-100.0,
        elbo=None,
        iteration_count=10,
        converged=True,
        created_at=NOW,
    )
    engine = DinaEngine()

    posterior = engine.profile_posterior(model, _batch("learner_x", (True, False)))
    diagnosis = engine.infer(model, _batch("learner_x", (True, False)))

    assert sum(posterior.values()) == pytest.approx(1.0)
    assert diagnosis.status == "estimated"
    assert diagnosis.concept_mastery["concept_1"] > 0.5
    assert diagnosis.concept_mastery["concept_2"] < 0.5


def test_high_dimensional_connected_q_matrix_uses_variational_inference() -> None:
    """Catch exponential profile enumeration for a connected 20-concept model."""

    from course_insight.modules.m5_learner_class_state.dina import DinaEngine

    cohort, q_matrix = _high_dimensional_case()
    engine = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=20,
        max_exact_concepts=12,
    )

    model = engine.fit(cohort, q_matrix)
    diagnosis = engine.infer(model, cohort[0])

    assert model.inference_mode == "variational"
    assert model.elbo is not None
    assert all(
        later + 1e-9 >= earlier
        for earlier, later in zip(
            model.objective_history,
            model.objective_history[1:],
            strict=False,
        )
    )
    assert diagnosis.status == "estimated"
    assert len(diagnosis.concept_mastery) == 20
    assert all(0.0 <= value <= 1.0 for value in diagnosis.concept_mastery.values())


def test_sqlite_repository_persists_observations_and_append_only_dina_models(
    tmp_path: Path,
) -> None:
    """Catch restart loss or silent replacement of DINA training evidence."""

    from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
    from course_insight.modules.m5_learner_class_state.dina import DinaEngine

    repository = SQLiteM5Repository(tmp_path / "m5-dina.sqlite3")
    repository.initialize()
    cohort = _training_cohort()
    for batch in cohort:
        repository.insert_or_get_learning_observation_batch(batch)
    engine = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=30,
    )
    model = engine.fit(cohort, _q_matrix())

    stored = repository.insert_or_get_dina_model(model)
    recovered_observations = repository.list_learning_observations(
        course_id="course_1",
        class_id="class_1",
    )
    recovered_model = repository.get_dina_model(
        course_id="course_1",
        model_version=model.model_version,
    )

    assert stored == model
    assert recovered_model == model
    expected_observations = sorted(
        (item for batch in cohort for item in batch.observations),
        key=lambda item: (item.occurred_at, item.attempt_id, item.observation_id),
    )
    assert [item.observation_id for item in recovered_observations] == [
        item.observation_id for item in expected_observations
    ]
    with pytest.raises(RuntimeError, match="DINA model conflict"):
        repository.insert_or_get_dina_model(
            model.model_copy(update={"observation_count": model.observation_count + 1})
        )


def test_m5_service_trains_persists_and_applies_dina(tmp_path: Path) -> None:
    """Catch a DINA engine that is never connected to the M5 service boundary."""

    from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
    from course_insight.modules.m5_learner_class_state.aggregation import (
        DeterministicClassAggregationPolicy,
    )
    from course_insight.modules.m5_learner_class_state.dina import DinaEngine
    from course_insight.modules.m5_learner_class_state.service import M5StateService
    from course_insight.modules.m5_learner_class_state.update_policy import (
        DeterministicStateUpdatePolicy,
    )

    repository = SQLiteM5Repository(tmp_path / "m5-dina-service.sqlite3")
    repository.initialize()
    engine = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=30,
    )
    service = M5StateService(
        repository,
        DeterministicStateUpdatePolicy(),
        DeterministicClassAggregationPolicy(),
        dina_engine=engine,
    )

    model = service.fit_dina_model(_training_cohort(), _q_matrix())
    diagnosis = service.infer_dina(model, _batch("learner_new", (True, False)))

    assert repository.get_dina_model(
        course_id=model.course_id,
        model_version=model.model_version,
    ) == model
    assert diagnosis.model_version == model.model_version
    assert diagnosis.status == "estimated"
