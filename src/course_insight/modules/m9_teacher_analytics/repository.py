"""M9 repository boundary for analytics and review decisions."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)


_REVIEW_TABLE = "m9_teacher_reviews"


class M9Repository(Protocol):
    """Persistence operations owned exclusively by M9."""

    def save_analytics(self, bundle: TeacherAnalyticsBundle) -> None:
        """Persist one generated teacher-analytics bundle."""

    def save_review_decision(self, decision: TeacherReviewDecision) -> None:
        """Persist one optimistic-version teacher decision."""

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        """Load one teacher decision by stable identity."""
