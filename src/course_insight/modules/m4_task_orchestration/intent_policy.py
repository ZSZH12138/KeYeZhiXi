"""Threshold policy for private M4 intent predictions."""

from __future__ import annotations

from dataclasses import dataclass

from course_insight.modules.m4_task_orchestration.intent import (
    IntentPrediction,
    IntentStatus,
)


@dataclass(frozen=True, slots=True)
class IntentDecisionOutcome:
    """Immutable policy result, containing only a supported task label or none."""

    label: str | None
    status: IntentStatus
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntentPolicy:
    """Accept an adapter candidate only when both confidence guards pass."""

    min_confidence: float
    min_margin: float

    def __post_init__(self) -> None:
        _validate_threshold("min_confidence", self.min_confidence)
        _validate_threshold("min_margin", self.min_margin)

    def accept(self, prediction: IntentPrediction) -> IntentDecisionOutcome:
        """Return the policy outcome without mutating the source prediction."""

        if prediction.status is IntentStatus.OUT_OF_SCOPE:
            return IntentDecisionOutcome(
                label=None,
                status=IntentStatus.OUT_OF_SCOPE,
                reason_codes=prediction.reason_codes,
            )
        if prediction.status is IntentStatus.INVALID:
            return IntentDecisionOutcome(
                label=None,
                status=IntentStatus.INVALID,
                reason_codes=prediction.reason_codes,
            )
        if prediction.status is IntentStatus.ABSTAINED:
            return IntentDecisionOutcome(
                label=None,
                status=IntentStatus.ABSTAINED,
                reason_codes=prediction.reason_codes,
            )

        reasons = prediction.reason_codes
        if prediction.confidence < self.min_confidence:
            reasons = (*reasons, "below_min_confidence")
        if prediction.margin < self.min_margin:
            reasons = (*reasons, "below_min_margin")
        if reasons != prediction.reason_codes:
            return IntentDecisionOutcome(
                label=None,
                status=IntentStatus.ABSTAINED,
                reason_codes=reasons,
            )
        return IntentDecisionOutcome(
            label=prediction.label,
            status=IntentStatus.ACCEPTED,
            reason_codes=prediction.reason_codes,
        )


def _validate_threshold(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between zero and one")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must be between zero and one")
