"""Deterministic class aggregation from each learner's latest state."""

from __future__ import annotations

import math

from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import (
    ClassConceptStatus,
    ClassMisconceptionSummary,
    ClassStateSnapshot,
    LearnerStateSnapshot,
    MasteryDistribution,
)
from course_insight.modules.m5_learner_class_state.update_policy import StatePolicy


class DeterministicClassAggregationPolicy:
    """Aggregate one current contribution for every assessed learner."""

    def aggregate_all(
        self,
        learner_states: list[LearnerStateSnapshot],
        policy: StatePolicy,
        *,
        class_version: int,
        previous: ClassStateSnapshot | None = None,
    ) -> ClassStateSnapshot:
        if class_version < 1 or not learner_states:
            raise DomainError(
                code="CLASS_AGGREGATION_INVALID",
                module="m5",
                message="class aggregation needs learners and a positive version",
            )
        by_learner = {state.learner_id: state for state in learner_states}
        if len(by_learner) != len(learner_states):
            raise DomainError(
                code="CLASS_AGGREGATION_INVALID",
                module="m5",
                message="class aggregation received duplicate learner states",
            )
        states = [by_learner[key] for key in sorted(by_learner)]
        course_id = states[0].course_id
        class_id = states[0].class_id
        if class_id != policy.class_id or any(
            (state.course_id, state.class_id) != (course_id, class_id)
            for state in states
        ):
            raise DomainError(
                code="CLASS_AGGREGATION_INVALID",
                module="m5",
                message="learner states must share the policy teaching scope",
            )
        assessed_count = len(states)
        if assessed_count > policy.class_size:
            raise DomainError(
                code="CLASS_AGGREGATION_INVALID",
                module="m5",
                message="assessed learners exceed the configured class size",
            )

        previous_by_concept = (
            {item.concept_id: item for item in previous.concept_status}
            if previous is not None
            else {}
        )
        concept_ids = sorted(
            {
                concept.concept_id
                for state in states
                for concept in state.concept_states
            }
        )
        concept_status = []
        for concept_id in concept_ids:
            samples = [
                concept
                for state in states
                for concept in state.concept_states
                if concept.concept_id == concept_id
            ]
            bands = [
                sample.band(
                    mastered=policy.mastered_threshold,
                    consolidating=policy.consolidating_threshold,
                )
                for sample in samples
            ]
            sample_count = len(samples)
            old = previous_by_concept.get(concept_id)
            mean_mastery = math.fsum(
                sample.mastery_probability for sample in samples
            ) / sample_count
            concept_status.append(
                ClassConceptStatus(
                    concept_id=concept_id,
                    mastery_distribution=MasteryDistribution(
                        mastered=bands.count("mastered") / sample_count,
                        consolidating=bands.count("consolidating") / sample_count,
                        priority_support=(
                            bands.count("priority_support") / sample_count
                        ),
                    ),
                    mean_mastery_probability=mean_mastery,
                    mean_confidence=math.fsum(
                        sample.mastery_confidence for sample in samples
                    )
                    / sample_count,
                    mastery_trend_delta=(
                        mean_mastery - old.mean_mastery_probability
                        if old is not None
                        else None
                    ),
                    trend_comparable=old is not None,
                    sample_count=sample_count,
                )
            )

        misconception_ids = sorted(
            {
                item.misconception_id
                for state in states
                for concept in state.concept_states
                for item in concept.misconceptions
            }
        )
        misconception_summary = []
        for misconception_id in misconception_ids:
            affected = []
            for state in states:
                matches = [
                    item
                    for concept in state.concept_states
                    for item in concept.misconceptions
                    if item.misconception_id == misconception_id
                    and item.is_active(
                        policy.misconception_activation_threshold
                    )
                ]
                if matches:
                    affected.append(max(item.evidence_count for item in matches))
            if affected:
                misconception_summary.append(
                    ClassMisconceptionSummary(
                        misconception_id=misconception_id,
                        affected_count=len(affected),
                        affected_rate_among_assessed=(
                            len(affected) / assessed_count
                        ),
                        evidence_attempts=max(
                            len(affected),
                            sum(affected),
                        ),
                    )
                )

        coverage = assessed_count / policy.class_size
        sufficient = (
            assessed_count >= policy.minimum_assessed_count
            and coverage >= policy.minimum_coverage
        )
        return ClassStateSnapshot(
            snapshot_id=f"{class_id}_state_v{class_version}",
            course_id=course_id,
            class_id=class_id,
            aggregation_policy_version=policy.aggregation_policy_version,
            scope={
                "mode": "latest_learner_states",
                "class_version": str(class_version),
            },
            class_size=policy.class_size,
            assessed_count=assessed_count,
            coverage_rate=coverage,
            concept_status=concept_status,
            misconception_summary=misconception_summary,
            evidence_status="sufficient" if sufficient else "insufficient",
            model_run_ids=sorted(
                {
                    state.model_run_id
                    for state in states
                    if state.model_run_id is not None
                }
            ),
            updated_at=max(state.updated_at for state in states),
        )

    def aggregate(
        self,
        learner: LearnerStateSnapshot,
        previous: ClassStateSnapshot | None,
        policy: StatePolicy,
    ) -> ClassStateSnapshot:
        """Compatibility wrapper for consumers without repository history."""

        return self.aggregate_all(
            [learner],
            policy,
            class_version=learner.state_version,
            previous=previous,
        )
