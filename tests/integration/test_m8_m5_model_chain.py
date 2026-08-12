"""M8 scoring observations must drive real M5 model-backed state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from course_insight.contracts.learning_models import ConceptResponseSequence
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
