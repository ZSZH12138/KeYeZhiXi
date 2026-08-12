"""M5 class aggregation uses one current contribution per learner."""

import pytest

from course_insight.contracts.state import ConceptState, LearnerStateSnapshot
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import StatePolicy
from tests.factories.m5_m8 import UTC_TIME


def _policy() -> StatePolicy:
    return StatePolicy(
        aggregation_policy_version="1.0.0",
        class_id="class_1",
        class_size=2,
        consolidating_threshold=0.4,
        mastered_threshold=0.8,
        minimum_assessed_count=1,
        minimum_coverage=0.0,
        misconception_activation_threshold=0.5,
    )


def _learner(learner_id: str, mastery: float) -> LearnerStateSnapshot:
    return LearnerStateSnapshot(
        snapshot_id=f"{learner_id}_state_v1",
        course_id="course_1",
        class_id="class_1",
        learner_id=learner_id,
        state_version=1,
        concept_states=[
            ConceptState(
                concept_id="concept_1",
                mastery_probability=mastery,
                mastery_confidence=0.75,
                misconceptions=[],
                hint_dependency=0.0,
                recent_correction_rate=mastery,
                evidence_count=1,
                updated_at=UTC_TIME,
            )
        ],
        overall_mastery=mastery,
        evidence_count=1,
        updated_at=UTC_TIME,
    )


def test_returning_learner_replaces_only_own_class_contribution() -> None:
    result = DeterministicClassAggregationPolicy().aggregate_all(
        [_learner("A", 0.4), _learner("B", 0.8)],
        _policy(),
        class_version=3,
    )

    assert result.concept_status[0].mean_mastery_probability == pytest.approx(0.6)
    assert result.assessed_count == 2


def test_two_class_versions_never_share_snapshot_id() -> None:
    aggregator = DeterministicClassAggregationPolicy()

    first = aggregator.aggregate_all(
        [_learner("A", 0.2)],
        _policy(),
        class_version=1,
    )
    second = aggregator.aggregate_all(
        [_learner("A", 0.2), _learner("B", 0.8)],
        _policy(),
        class_version=2,
    )

    assert first.snapshot_id != second.snapshot_id
