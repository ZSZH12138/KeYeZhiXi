"""Versioned, finite-only feature construction for private M6 policies."""

from __future__ import annotations

import math

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    TASK_TYPES,
    TUTORING_STATES,
    TutoringPolicyContext,
)


class FeatureBuilder:
    """Build the fixed-order structured ``m6-features-v1`` vector."""

    schema_version = "m6-features-v1"

    def build(self, context: TutoringPolicyContext) -> tuple[float, ...]:
        """Return only finite values derived from existing structured fields."""

        signals = context.signals
        vector = (
            *(float(context.current_state == state) for state in TUTORING_STATES),
            *(float(context.task_type == task_type) for task_type in TASK_TYPES),
            float(context.turn_count),
            float(context.score_ratio),
            float(context.target_concept_count),
            float(signals.needs_teacher_review),
            float(signals.has_diagnosed_misconception),
            float(signals.has_active_misconception),
            float(signals.has_prerequisite_gap),
            float(signals.has_new_evidence),
            float(signals.minimum_recent_correction_rate),
            float(signals.minimum_mastery_confidence),
            float(signals.maximum_hint_dependency),
            float(context.learner_evidence_count),
        )
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("m6 feature vector must contain only finite values")
        return vector
