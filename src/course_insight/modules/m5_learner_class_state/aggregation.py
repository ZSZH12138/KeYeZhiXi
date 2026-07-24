"""Deterministic incremental class aggregation policy."""

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
    """Aggregate learners into a transparent class state incrementally."""

    def aggregate(
        self,
        learner: LearnerStateSnapshot,
        previous: ClassStateSnapshot | None,
        policy: StatePolicy,
    ) -> ClassStateSnapshot:
        """Aggregate one learner, merging with any prior class snapshot.

        M5-04: When *previous* is not ``None`` the new learner is folded into
        the running aggregate instead of replacing it.  New learners increment
        ``assessed_count``; returning learners keep the count but refresh
        their concept values and trend deltas.
        """

        previous_by_concept = (
            {item.concept_id: item for item in previous.concept_status}
            if previous is not None
            else {}
        )
        previous_by_misconception = (
            {item.misconception_id: item for item in previous.misconception_summary}
            if previous is not None
            else {}
        )
        assessed_learners: set[str] = set()
        if previous is not None:
            assessed_learners = {
                key
                for key, value in previous.scope.items()
                if key != "mode" and value == "assessed"
            }
        is_new_learner = learner.learner_id not in assessed_learners
        prev_assessed = previous.assessed_count if previous is not None else 0
        new_assessed = prev_assessed + (1 if is_new_learner else 0)

        concept_status: list[ClassConceptStatus] = []
        for state in learner.concept_states:
            band = state.band(
                mastered=policy.mastered_threshold,
                consolidating=policy.consolidating_threshold,
            )
            old = previous_by_concept.get(state.concept_id)
            old_count = old.sample_count if old is not None else 0

            if old is not None and old_count > 0 and is_new_learner:
                # M5-04: incremental running average for a new learner.
                new_count = old_count + 1
                new_mean = (
                    old.mean_mastery_probability * old_count
                    + state.mastery_probability
                ) / new_count
                new_confidence = (
                    old.mean_confidence * old_count
                    + state.mastery_confidence
                ) / new_count
                old_mastered = old.mastery_distribution.mastered * old_count
                old_consolidating = (
                    old.mastery_distribution.consolidating * old_count
                )
                old_priority = (
                    old.mastery_distribution.priority_support * old_count
                )
                proportions = {
                    "mastered": (
                        old_mastered + (1.0 if band == "mastered" else 0.0)
                    ) / new_count,
                    "consolidating": (
                        old_consolidating
                        + (1.0 if band == "consolidating" else 0.0)
                    ) / new_count,
                    "priority_support": (
                        old_priority
                        + (1.0 if band == "priority_support" else 0.0)
                    ) / new_count,
                }
                trend_delta = new_mean - old.mean_mastery_probability
                trend_comparable = True
                sample_count = new_count
            elif old is not None and old_count > 0:
                # Returning learner: keep count, refresh values.
                new_count = old_count
                new_mean = state.mastery_probability
                new_confidence = state.mastery_confidence
                proportions = {
                    "mastered": 1.0 if band == "mastered" else 0.0,
                    "consolidating": 1.0 if band == "consolidating" else 0.0,
                    "priority_support": 1.0 if band == "priority_support" else 0.0,
                }
                trend_delta = new_mean - old.mean_mastery_probability
                trend_comparable = True
                sample_count = new_count
            else:
                # First assessment for this concept.
                proportions = {
                    "mastered": 1.0 if band == "mastered" else 0.0,
                    "consolidating": 1.0 if band == "consolidating" else 0.0,
                    "priority_support": 1.0 if band == "priority_support" else 0.0,
                }
                new_mean = state.mastery_probability
                new_confidence = state.mastery_confidence
                trend_delta = (
                    state.mastery_probability - old.mean_mastery_probability
                    if old is not None
                    else None
                )
                trend_comparable = old is not None
                sample_count = 1

            concept_status.append(
                ClassConceptStatus(
                    concept_id=state.concept_id,
                    mastery_distribution=MasteryDistribution(**proportions),
                    mean_mastery_probability=new_mean,
                    mean_confidence=new_confidence,
                    mastery_trend_delta=trend_delta,
                    trend_comparable=trend_comparable,
                    sample_count=sample_count,
                )
            )

        active = [
            item
            for state in learner.concept_states
            for item in state.misconceptions
            if item.is_active(policy.misconception_activation_threshold)
        ]
        new_misconception_ids = {item.misconception_id for item in active}
        all_misconception_ids = set(previous_by_misconception) | new_misconception_ids
        misconception_summary = []
        for mid in sorted(all_misconception_ids):
            old_summary = previous_by_misconception.get(mid)
            is_new = mid in new_misconception_ids
            old_affected = old_summary.affected_count if old_summary else 0
            old_evidence = old_summary.evidence_attempts if old_summary else 0
            new_affected = old_affected + (1 if is_new and is_new_learner else 0)
            if is_new:
                new_evidence = max(
                    old_evidence,
                    max(
                        (item.evidence_count for item in active if item.misconception_id == mid),
                        default=1,
                    ),
                )
            else:
                new_evidence = old_evidence
            affected_rate = (
                new_affected / new_assessed if new_assessed > 0 else 0.0
            )
            misconception_summary.append(
                ClassMisconceptionSummary(
                    misconception_id=mid,
                    affected_count=new_affected,
                    affected_rate_among_assessed=affected_rate,
                    evidence_attempts=max(1, new_evidence),
                )
            )

        coverage = (
            new_assessed / policy.class_size if policy.class_size > 0 else 0.0
        )
        sufficient = (
            new_assessed >= policy.minimum_assessed_count
            and coverage >= policy.minimum_coverage
        )
        if previous is not None:
            scope = {**previous.scope, learner.learner_id: "assessed"}
        else:
            scope = {"mode": "incremental", learner.learner_id: "assessed"}
        return ClassStateSnapshot(
            snapshot_id=f"{learner.class_id}_state_v{learner.state_version}",
            course_id=learner.course_id,
            class_id=learner.class_id,
            aggregation_policy_version=policy.aggregation_policy_version,
            scope=scope,
            class_size=policy.class_size,
            assessed_count=new_assessed,
            coverage_rate=coverage,
            concept_status=concept_status,
            misconception_summary=misconception_summary,
            evidence_status="sufficient" if sufficient else "insufficient",
            updated_at=learner.updated_at,
        )
