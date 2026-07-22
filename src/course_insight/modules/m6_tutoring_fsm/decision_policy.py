"""Deterministic, versioned tutoring decisions built on the M6 state graph."""

from __future__ import annotations

from dataclasses import dataclass
import math

from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)


@dataclass(frozen=True, slots=True)
class DecisionSignals:
    """A frozen summary of the evidence used by one policy decision."""

    needs_teacher_review: bool
    has_diagnosed_misconception: bool
    has_active_misconception: bool
    has_prerequisite_gap: bool
    has_new_evidence: bool
    minimum_recent_correction_rate: float
    minimum_mastery_confidence: float
    maximum_hint_dependency: float

    def __post_init__(self) -> None:
        for field_name in (
            "minimum_recent_correction_rate",
            "minimum_mastery_confidence",
            "maximum_hint_dependency",
        ):
            _require_probability(getattr(self, field_name), field_name)

    def needs_remediation(self) -> bool:
        """Return whether any governed signal requires minimal remediation."""

        return any(
            (
                self.needs_teacher_review,
                self.has_active_misconception,
                self.has_prerequisite_gap,
            )
        )


@dataclass(frozen=True, slots=True)
class M6DecisionPolicy:
    """Versioned thresholds and the complete deterministic S0-S5 policy."""

    policy_version: str = "m6-deterministic-v1"
    weak_mastery_threshold: float = 0.8
    stable_correction_threshold: float = 0.8
    mastery_confidence_threshold: float = 0.6
    maximum_hint_dependency: float = 0.0
    active_misconception_threshold: float = 0.5

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be blank")
        for field_name in (
            "weak_mastery_threshold",
            "stable_correction_threshold",
            "mastery_confidence_threshold",
            "maximum_hint_dependency",
            "active_misconception_threshold",
        ):
            _require_probability(getattr(self, field_name), field_name)

    def decide_next_state(
        self,
        current_state: str,
        signals: DecisionSignals,
    ) -> str:
        """Choose a candidate from evidence, then validate it against the graph."""

        if current_state == "S0":
            candidate = "S1"
        elif current_state == "S1":
            candidate = "S2" if signals.needs_remediation() else "S3"
        elif current_state == "S2":
            candidate = "S3"
        elif current_state == "S3":
            candidate = "S4"
        elif current_state == "S4":
            candidate = self._decide_from_practice(signals)
        else:
            candidate = ""

        DEFAULT_STATE_MACHINE.validate_transition(current_state, candidate)
        return candidate

    def _decide_from_practice(self, signals: DecisionSignals) -> str:
        """Apply remediation and stability gates after self-explanation."""

        if signals.needs_remediation():
            return "S2"
        if (
            signals.has_diagnosed_misconception
            or not signals.has_new_evidence
            or signals.minimum_recent_correction_rate
            < self.stable_correction_threshold
            or signals.minimum_mastery_confidence
            < self.mastery_confidence_threshold
            or signals.maximum_hint_dependency > self.maximum_hint_dependency
        ):
            return "S3"
        return "S5"


def _require_probability(value: float, field_name: str) -> None:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"{field_name} must be a finite probability")
