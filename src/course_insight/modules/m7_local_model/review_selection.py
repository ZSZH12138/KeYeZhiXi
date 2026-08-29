"""Fixed local confidence routing for DeepSeek scoring results."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from course_insight.contracts.assessment import RubricScoringResult, RubricScoringTask


@dataclass(frozen=True, slots=True)
class ReviewSelectionDecision:
    """Privacy-safe record of the fixed local threshold decision."""

    require_teacher_review: bool
    review_flags: tuple[str, ...]
    audit_flags: tuple[str, ...]
    reason_code: str
    selector_id: str = "m7-confidence-threshold-v1"
    raw_risk: float | None = None

    def __post_init__(self) -> None:
        expected = (
            ("teacher_review_required", "low_confidence")
            if self.require_teacher_review
            else ()
        )
        if self.review_flags != expected:
            raise ValueError("review flags must match the fixed confidence decision")
        if len(self.audit_flags) != len(set(self.audit_flags)):
            raise ValueError("review audit flags must be unique")
        if not self.reason_code or not self.selector_id:
            raise ValueError("review decision identity must not be blank")
        if self.raw_risk is not None and (
            not math.isfinite(self.raw_risk) or not 0.0 <= self.raw_risk <= 1.0
        ):
            raise ValueError("review risk must be between zero and one")

    def safe_record(self) -> dict[str, Any]:
        return {
            "review_selection_mode": "confidence_threshold",
            "review_selection_effective_mode": "confidence_threshold",
            "review_selection_reason": self.reason_code,
            "review_selector_id": self.selector_id,
            "review_selector_artifact_sha256": None,
            "review_candidate_accepted": not self.require_teacher_review,
        }


class ConfidenceThresholdReviewSelector:
    """Require review exactly when confidence is below the task threshold."""

    selector_id = "m7-confidence-threshold-v1"

    def select(
        self,
        *,
        task: RubricScoringTask,
        result: RubricScoringResult,
    ) -> ReviewSelectionDecision:
        requires_review = result.confidence < task.review_confidence_threshold
        return ReviewSelectionDecision(
            require_teacher_review=requires_review,
            review_flags=(
                ("teacher_review_required", "low_confidence")
                if requires_review
                else ()
            ),
            audit_flags=(
                "review_selector_confidence_threshold",
                (
                    "review_selector_deferred"
                    if requires_review
                    else "review_selector_accepted"
                ),
            ),
            reason_code=(
                "confidence_below_0_5"
                if requires_review
                else "confidence_at_least_0_5"
            ),
            raw_risk=1.0 - result.confidence,
        )


__all__ = [
    "ConfidenceThresholdReviewSelector",
    "ReviewSelectionDecision",
]
