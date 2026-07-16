"""Deterministic zero-argument M9 service stub."""

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


class _MemoryM9Repository:
    def __init__(self) -> None:
        self.analytics: dict[str, TeacherAnalyticsBundle] = {}
        self.decisions: dict[str, TeacherReviewDecision] = {}

    def save_analytics(self, bundle: TeacherAnalyticsBundle) -> None:
        self.analytics = {
            **self.analytics,
            bundle.report_id: bundle.model_copy(deep=True),
        }

    def save_review_decision(self, decision: TeacherReviewDecision) -> None:
        self.decisions = {
            **self.decisions,
            decision.decision_id: decision.model_copy(deep=True),
        }

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        decision = self.decisions.get(decision_id)
        return decision.model_copy(deep=True) if decision is not None else None


class M9TeacherAnalyticsServiceStub(M9TeacherAnalyticsService):
    """Instantiate M9 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(_MemoryM9Repository(), object(), object())
