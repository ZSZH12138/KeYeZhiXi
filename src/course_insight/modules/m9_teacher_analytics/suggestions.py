"""Threshold policy and deterministic evidence-backed M9 suggestions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from course_insight.contracts.analytics import TeachingSuggestion
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import StateUpdateResult
from course_insight.infrastructure.json_io import read_json


@dataclass(frozen=True)
class TeacherThresholdPolicy:
    minimum_coverage: float
    minimum_assessed_count: int
    minimum_confidence: float
    weak_mastery_threshold: float
    misconception_threshold: float
    priority_support_threshold: float

    @classmethod
    def from_path(cls, path: Path) -> "TeacherThresholdPolicy":
        """Load the strict local policy JSON."""

        payload = read_json(path)
        if type(payload) is not dict:
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="teacher threshold policy must be a JSON object",
            )
        expected = {
            "minimum_coverage",
            "minimum_assessed_count",
            "minimum_confidence",
            "weak_mastery_threshold",
            "misconception_threshold",
            "priority_support_threshold",
        }
        if set(payload) != expected:
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="teacher threshold policy fields are invalid",
            )
        policy = cls(**payload)
        policy.validate()
        return policy

    def validate(self) -> None:
        """Reject non-finite thresholds and invalid minimum counts."""

        thresholds = (
            self.minimum_coverage,
            self.minimum_confidence,
            self.weak_mastery_threshold,
            self.misconception_threshold,
            self.priority_support_threshold,
        )
        if any(
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
            for value in thresholds
        ) or type(self.minimum_assessed_count) is not int or self.minimum_assessed_count < 0:
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="teacher threshold policy values are invalid",
            )


def _mean_confidence(state: StateUpdateResult) -> float:
    values = [
        status.mean_confidence
        for status in state.class_state_snapshot.concept_status
    ]
    return math.fsum(values) / len(values) if values else 0.0


def build_teaching_suggestions(
    state: StateUpdateResult,
    policy: TeacherThresholdPolicy,
) -> list[TeachingSuggestion]:
    """Build only conservative suggestions when class evidence is insufficient."""

    snapshot = state.class_state_snapshot
    evidence_ids = list(state.processed_audit_ids)
    concept_ids = list(
        dict.fromkeys(
            [status.concept_id for status in snapshot.concept_status]
            or [
                concept.concept_id
                for concept in state.learner_state_snapshot.concept_states
            ]
        )
    )
    if not evidence_ids or not concept_ids:
        return []
    confidence = _mean_confidence(state)
    if not snapshot.is_actionable(
        policy.minimum_coverage,
        policy.minimum_assessed_count,
    ):
        return [
            TeachingSuggestion(
                suggestion_id=f"suggestion_{snapshot.snapshot_id}_collect",
                action_type="collect",
                concept_ids=concept_ids,
                content=(
                    "Collect additional aligned assessment evidence before "
                    "changing class instruction."
                ),
                trigger_metrics={
                    "coverage_rate": snapshot.coverage_rate,
                    "assessed_count": float(snapshot.assessed_count),
                },
                affected_count=snapshot.assessed_count,
                affected_rate=snapshot.coverage_rate,
                coverage_rate=snapshot.coverage_rate,
                confidence=confidence,
                evidence_ids=evidence_ids,
                status="candidate",
            )
        ]

    suggestions: list[TeachingSuggestion] = []
    for status in snapshot.concept_status:
        if not status.needs_support(policy.priority_support_threshold):
            continue
        affected_rate = status.mastery_distribution.priority_support
        affected_count = min(
            snapshot.assessed_count,
            max(1, round(affected_rate * snapshot.assessed_count)),
        )
        suggestions.append(
            TeachingSuggestion(
                suggestion_id=(
                    f"suggestion_{snapshot.snapshot_id}_{status.concept_id}"
                ),
                action_type="targeted_support",
                concept_ids=[status.concept_id],
                content="Provide a targeted review using the assessed course concept.",
                trigger_metrics={"priority_support_rate": affected_rate},
                affected_count=affected_count,
                affected_rate=affected_rate,
                coverage_rate=snapshot.coverage_rate,
                confidence=status.mean_confidence,
                evidence_ids=evidence_ids,
                status=(
                    "candidate"
                    if status.mean_confidence >= policy.minimum_confidence
                    else "observe"
                ),
            )
        )
    if suggestions:
        return suggestions
    return [
        TeachingSuggestion(
            suggestion_id=f"suggestion_{snapshot.snapshot_id}_observe",
            action_type="observe",
            concept_ids=concept_ids,
            content="Continue observing the existing class evidence trend.",
            trigger_metrics={"coverage_rate": snapshot.coverage_rate},
            affected_count=snapshot.assessed_count,
            affected_rate=snapshot.coverage_rate,
            coverage_rate=snapshot.coverage_rate,
            confidence=confidence,
            evidence_ids=evidence_ids,
            status="candidate",
        )
    ]
