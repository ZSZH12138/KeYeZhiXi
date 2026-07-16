"""Deterministic one-learner class aggregation policy."""

from __future__ import annotations

from course_insight.contracts.state import (
    ClassConceptStatus,
    ClassMisconceptionSummary,
    ClassStateSnapshot,
    LearnerStateSnapshot,
    MasteryDistribution,
)
from course_insight.modules.m5_learner_class_state.update_policy import StatePolicy


class DeterministicClassAggregationPolicy:
    """Expose coverage and evidence sufficiency without opaque inference."""

    def aggregate(
        self,
        learner: LearnerStateSnapshot,
        previous: ClassStateSnapshot | None,
        policy: StatePolicy,
    ) -> ClassStateSnapshot:
        """Aggregate one pseudonymous learner into a transparent class state."""

        previous_by_concept = (
            {item.concept_id: item for item in previous.concept_status}
            if previous is not None
            else {}
        )
        concept_status: list[ClassConceptStatus] = []
        for state in learner.concept_states:
            band = state.band(
                mastered=policy.mastered_threshold,
                consolidating=policy.consolidating_threshold,
            )
            proportions = {
                "mastered": 1.0 if band == "mastered" else 0.0,
                "consolidating": 1.0 if band == "consolidating" else 0.0,
                "priority_support": 1.0 if band == "priority_support" else 0.0,
            }
            old = previous_by_concept.get(state.concept_id)
            concept_status.append(
                ClassConceptStatus(
                    concept_id=state.concept_id,
                    mastery_distribution=MasteryDistribution(**proportions),
                    mean_mastery_probability=state.mastery_probability,
                    mean_confidence=state.mastery_confidence,
                    mastery_trend_delta=(
                        state.mastery_probability - old.mean_mastery_probability
                        if old is not None
                        else None
                    ),
                    trend_comparable=old is not None,
                    sample_count=1,
                )
            )
        active = [
            item
            for state in learner.concept_states
            for item in state.misconceptions
            if item.is_active(policy.misconception_activation_threshold)
        ]
        misconception_summary = [
            ClassMisconceptionSummary(
                misconception_id=item.misconception_id,
                affected_count=1,
                affected_rate_among_assessed=1.0,
                evidence_attempts=max(1, item.evidence_count),
            )
            for item in sorted(active, key=lambda value: value.misconception_id)
        ]
        coverage = 1 / policy.class_size
        sufficient = (
            1 >= policy.minimum_assessed_count
            and coverage >= policy.minimum_coverage
        )
        return ClassStateSnapshot(
            snapshot_id=f"{learner.class_id}_state_v{learner.state_version}",
            course_id=learner.course_id,
            class_id=learner.class_id,
            aggregation_policy_version=policy.aggregation_policy_version,
            scope={"mode": "placeholder", "learner_id": learner.learner_id},
            class_size=policy.class_size,
            assessed_count=1,
            coverage_rate=coverage,
            concept_status=concept_status,
            misconception_summary=misconception_summary,
            evidence_status="sufficient" if sufficient else "insufficient",
            updated_at=learner.updated_at,
        )
