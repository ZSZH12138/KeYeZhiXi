"""M8 scoring observations must drive real M5 model-backed state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from course_insight.contracts.learning_models import (
    ConceptResponseSequence,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.application.coordinator import AppCoordinator
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.bkt import BktEngine
from course_insight.modules.m5_learner_class_state.dina import DinaEngine
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
)
from course_insight.modules.m8_assessment_scoring.observation_builder import (
    build_observation_batch,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from tests.factories.m5_m8 import (
    make_knowledge_bundle,
    make_paper,
    make_scoring_bundle,
)
from tests.unit.test_m5_bkt import _sequence
from tests.unit.test_m5_dina import _q_matrix, _training_cohort


def _state_policy(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "aggregation_policy_version": "model-state-v1",
                "class_id": "class_1",
                "class_size": 1,
                "consolidating_threshold": 0.4,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 1,
                "minimum_coverage": 1.0,
                "misconception_activation_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_m8_observation_runs_models_and_bkt_drives_m5_mastery(
    tmp_path: Path,
) -> None:
    """Catch empty model runs or score-ratio mastery after models are available."""

    repository = SQLiteM5Repository(tmp_path / "model-chain.sqlite3")
    repository.initialize()
    service = M5StateService(
        repository,
        DeterministicStateUpdatePolicy(),
        DeterministicClassAggregationPolicy(),
        dina_engine=DinaEngine(
            min_students=4,
            min_responses_per_item=4,
            max_iterations=30,
        ),
        bkt_engine=BktEngine(
            min_students=4,
            min_observations_per_student=5,
            max_iterations=50,
        ),
    )
    service.fit_dina_model(_training_cohort(), _q_matrix())
    bkt_sequences: list[ConceptResponseSequence] = [
        _sequence(
            f"learner_{index}",
            outcomes,
            concept_id="concept_2",
        )
        for index, outcomes in enumerate(
            (
                [True, True, False, True, True],
                [False, True, True, True, False],
                [False, False, True, True, True],
                [True, False, False, True, False],
            ),
            start=1,
        )
    ]
    service.fit_bkt_model(bkt_sequences)

    knowledge = make_knowledge_bundle(subjective=False)
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper)
    observations = build_observation_batch(
        FrozenAssessmentRecord(
            paper=paper,
            course_id="course_1",
            class_id="class_1",
            frozen_rubrics=[],
        ),
        scoring,
    )

    models = service.run_learning_models(observations, knowledge)
    state = service.update_state(
        scoring_result_bundle=scoring,
        knowledge_bundle=knowledge,
        previous_learner_state_snapshot=None,
        previous_class_state_snapshot=None,
        state_policy_path=_state_policy(tmp_path / "state.json"),
        learning_observation_batch=observations,
        learning_model_run=models,
    )

    bkt_mastery = models.knowledge_trace.concept_probabilities["concept_2"]
    concept_state = state.learner_state_snapshot.get_concept_state("concept_2")
    assert models.status == "completed"
    assert models.diagnosis.status == "estimated"
    assert models.knowledge_trace.status == "estimated"
    assert concept_state.mastery_probability == pytest.approx(bkt_mastery)
    assert state.learner_state_snapshot.model_run_id == models.run_id
    assert state.learner_state_snapshot.dina_model_version == (
        models.diagnosis.model_version
    )
    assert state.learner_state_snapshot.bkt_model_version == (
        models.knowledge_trace.model_version
    )
    assert state.class_state_snapshot.model_run_ids == [models.run_id]


def test_coordinator_forwards_real_observations_and_knowledge_to_m5() -> None:
    """Catch the application layer retaining the old empty-batch model call."""

    knowledge = make_knowledge_bundle(subjective=False)
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper)
    observations = build_observation_batch(
        FrozenAssessmentRecord(
            paper=paper,
            course_id="course_1",
            class_id="class_1",
            frozen_rubrics=[],
        ),
        scoring,
    )

    class CapturingM5:
        def __init__(self) -> None:
            self.received = None

        def run_learning_models(self, observation_batch, knowledge_bundle):
            self.received = (observation_batch, knowledge_bundle)
            return "model-run"

    m5 = CapturingM5()
    coordinator = object.__new__(AppCoordinator)
    coordinator._m5 = m5

    result = coordinator._run_learning_models(observations, knowledge)

    assert result == "model-run"
    assert m5.received == (observations, knowledge)


def test_learning_models_replay_complete_history_after_restart(
    tmp_path: Path,
) -> None:
    """Catch BKT restarting from its prior for every assessment batch."""

    repository = SQLiteM5Repository(tmp_path / "model-history.sqlite3")
    repository.initialize()
    dina_engine = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=30,
    )
    bkt_engine = BktEngine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=50,
    )
    service = M5StateService(
        repository,
        DeterministicStateUpdatePolicy(),
        DeterministicClassAggregationPolicy(),
        dina_engine=dina_engine,
        bkt_engine=bkt_engine,
    )
    service.fit_dina_model(_training_cohort(), _q_matrix())
    bkt_model = service.fit_bkt_model(
        [
            _sequence(
                f"training_learner_{index}",
                outcomes,
                concept_id="concept_2",
            )
            for index, outcomes in enumerate(
                (
                    [True, True, False, True, True],
                    [False, True, True, True, False],
                    [False, False, True, True, True],
                    [True, False, False, True, False],
                ),
                start=1,
            )
        ]
    )
    occurred_at = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)

    def batch(index: int, *, correct: bool) -> LearningObservationBatch:
        observation = LearningObservation(
            observation_id=f"history_observation_{index}",
            learner_id="history_learner",
            course_id="course_1",
            class_id="class_1",
            attempt_id=f"history_attempt_{index}",
            item_id="item_2",
            item_version="1.0.0",
            concept_ids=["concept_2"],
            score=1.0 if correct else 0.0,
            max_score=1.0,
            response_outcome="correct" if correct else "incorrect",
            outcome_policy_version="binary-policy-1",
            source_audit_id=f"history_audit_{index}",
            source_audit_version=1,
            occurred_at=occurred_at + timedelta(minutes=index),
        )
        return LearningObservationBatch(
            batch_id=f"history_batch_{index}",
            learner_id=observation.learner_id,
            observations=[observation],
            watermark=f"history_watermark_{index}",
            created_at=observation.occurred_at,
        )

    knowledge = make_knowledge_bundle(subjective=False)
    first_batch = batch(1, correct=True)
    second_batch = batch(2, correct=False)
    service.run_learning_models(first_batch, knowledge)
    second = service.run_learning_models(second_batch, knowledge)
    expected = bkt_engine.update(
        bkt_model,
        _sequence(
            "history_learner",
            [True, False],
            concept_id="concept_2",
        ),
    )

    assert second.observation_count == 2
    assert second.knowledge_trace.observation_count == 2
    assert second.knowledge_trace.concept_probabilities["concept_2"] == pytest.approx(
        expected.concept_probabilities["concept_2"]
    )

    restarted = M5StateService(
        repository,
        DeterministicStateUpdatePolicy(),
        DeterministicClassAggregationPolicy(),
        dina_engine=DinaEngine(),
        bkt_engine=BktEngine(),
    )
    replay = restarted.run_learning_models(second_batch, knowledge)

    assert replay == second
