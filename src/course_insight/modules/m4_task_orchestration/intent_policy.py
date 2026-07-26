"""Threshold policy for private M4 intent predictions."""

from __future__ import annotations

from dataclasses import dataclass

from course_insight.modules.m4_task_orchestration.intent import (
    IntentPrediction,
    IntentStatus,
    _normalize_reason_codes,
)


@dataclass(frozen=True, slots=True)
class IntentDecisionOutcome:
    """Immutable policy result, containing only a supported task label or none."""

    label: str | None
    status: IntentStatus
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_reason_codes(self.reason_codes),
        )


@dataclass(frozen=True, slots=True)
class IntentPolicy:
    """Accept an adapter candidate only when both confidence guards pass."""

    min_confidence: float
    min_margin: float
    fallback_to_rules: bool = True
    fail_closed: bool = True

    def __post_init__(self) -> None:
        _validate_threshold("min_confidence", self.min_confidence)
        _validate_threshold("min_margin", self.min_margin)
        if not isinstance(self.fallback_to_rules, bool):
            raise ValueError("fallback_to_rules must be a boolean")
        if not isinstance(self.fail_closed, bool):
            raise ValueError("fail_closed must be a boolean")

    def accept(self, prediction: IntentPrediction) -> IntentDecisionOutcome:
        """Return the policy outcome without mutating the source prediction."""

        if prediction.status is not IntentStatus.ACCEPTED:
            return IntentDecisionOutcome(
                label=None,
                status=prediction.status,
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
