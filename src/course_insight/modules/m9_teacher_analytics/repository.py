"""M9 repository boundary for analytics and review decisions."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)


_REVIEW_TABLE = "m9_teacher_reviews"


class ReviewDecisionConflictError(RuntimeError):
    """Another immutable teacher decision already won this audit version."""


class M9Repository(Protocol):
    """Persistence operations owned exclusively by M9."""

    def save_analytics(self, bundle: TeacherAnalyticsBundle) -> None:
        """Persist one generated teacher-analytics bundle."""

    def insert_or_get_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str,
    ) -> TeacherAnalyticsBundle:
        """Persist one scoped report or return its identical winner."""

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        """Load one report by stable identity."""

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        """Load the newest report within an exact teaching scope."""

    def insert_or_get_review_decision(
        self,
        decision: TeacherReviewDecision,
    ) -> TeacherReviewDecision:
        """Persist a decision idempotently or expose a content conflict."""

    def save_review_decision(self, decision: TeacherReviewDecision) -> None:
        """Persist one optimistic-version teacher decision."""

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        """Load one teacher decision by stable identity."""


__all__ = ["M9Repository", "ReviewDecisionConflictError"]
