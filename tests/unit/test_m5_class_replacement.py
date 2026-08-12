"""M5 class aggregation uses one current contribution per learner."""

from dataclasses import replace

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import (
    ConceptState,
    LearnerStateSnapshot,
    MisconceptionStrength,
)
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


def _learner(
    learner_id: str,
    mastery: float,
    *,
    course_id: str = "course_1",
    class_id: str = "class_1",
    misconceptions: list[MisconceptionStrength] | None = None,
) -> LearnerStateSnapshot:
    return LearnerStateSnapshot(
        snapshot_id=f"{learner_id}_state_v1",
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
        state_version=1,
        concept_states=[
            ConceptState(
                concept_id="concept_1",
                mastery_probability=mastery,
                mastery_confidence=0.75,
                misconceptions=(
                    [] if misconceptions is None else misconceptions
                ),
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


@pytest.mark.parametrize(
    ("learners", "class_version"),
    [([], 1), ([_learner("A", 0.5)], 0)],
)
def test_class_aggregation_requires_learners_and_positive_version(
    learners: list[LearnerStateSnapshot],
    class_version: int,
) -> None:
    with pytest.raises(DomainError) as captured:
        DeterministicClassAggregationPolicy().aggregate_all(
            learners,
            _policy(),
            class_version=class_version,
        )
    assert captured.value.code == "CLASS_AGGREGATION_INVALID"


def test_class_aggregation_rejects_duplicate_or_mixed_scope_states() -> None:
    aggregator = DeterministicClassAggregationPolicy()
    learner = _learner("A", 0.5)

    with pytest.raises(DomainError, match="duplicate"):
        aggregator.aggregate_all(
            [learner, learner.model_copy(deep=True)],
            _policy(),
            class_version=1,
        )
    with pytest.raises(DomainError, match="scope"):
        aggregator.aggregate_all(
            [learner, _learner("B", 0.5, class_id="class_2")],
            _policy(),
            class_version=1,
        )
    with pytest.raises(DomainError, match="class size"):
        aggregator.aggregate_all(
            [learner, _learner("B", 0.5)],
            replace(_policy(), class_size=1),
            class_version=1,
        )


def test_class_aggregation_counts_each_active_misconception_once() -> None:
    active = MisconceptionStrength(
        misconception_id="misconception_1",
        strength=0.8,
        evidence_count=3,
        last_seen_at=UTC_TIME,
    )
    inactive = active.model_copy(
        update={"strength": 0.2, "evidence_count": 1}
    )

    result = DeterministicClassAggregationPolicy().aggregate_all(
        [
            _learner("A", 0.4, misconceptions=[active]),
            _learner("B", 0.8, misconceptions=[inactive]),
        ],
        _policy(),
        class_version=4,
    )

    assert len(result.misconception_summary) == 1
    summary = result.misconception_summary[0]
    assert summary.misconception_id == "misconception_1"
    assert summary.affected_count == 1
    assert summary.affected_rate_among_assessed == pytest.approx(0.5)
    assert summary.evidence_attempts == 3
