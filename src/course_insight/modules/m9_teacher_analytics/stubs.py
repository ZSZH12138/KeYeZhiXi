"""Deterministic zero-argument M9 service stub."""

from collections.abc import Sequence

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
    analytics_learner_scope,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


class _MemoryM9Repository:
    def __init__(self) -> None:
        self.analytics: dict[str, TeacherAnalyticsBundle] = {}
        self.analytics_courses: dict[str, str] = {}
        self.analytics_learner_scopes: dict[str, tuple[str, ...]] = {}
        self.decisions: dict[str, TeacherReviewDecision] = {}
        self.model_audits: dict[str, M9ModelAuditRecord] = {}

    def save_model_audit(self, record: M9ModelAuditRecord) -> None:
        current = self.model_audits.get(record.invocation_id)
        if current is not None and current != record:
            raise RuntimeError("M9 model audit identity conflict")
        self.model_audits = {
            **self.model_audits,
            record.invocation_id: record,
        }

    def get_model_audit(
        self,
        invocation_id: str,
    ) -> M9ModelAuditRecord | None:
        return self.model_audits.get(invocation_id)

    def save_analytics(self, bundle: TeacherAnalyticsBundle) -> None:
        self.analytics = {
            **self.analytics,
            bundle.report_id: bundle.model_copy(deep=True),
        }

    def insert_or_get_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str,
        learner_scope_ids: Sequence[str] | None = None,
    ) -> TeacherAnalyticsBundle:
        learner_scope = analytics_learner_scope(bundle, learner_scope_ids)
        current = self.analytics.get(bundle.report_id)
        current_course = self.analytics_courses.get(bundle.report_id)
        current_scope = self.analytics_learner_scopes.get(bundle.report_id)
        if current is not None and (
            current != bundle
            or current_course != course_id
            or current_scope != learner_scope
        ):
            raise RuntimeError("M9 analytics report identity conflict")
        self.analytics[bundle.report_id] = bundle.model_copy(deep=True)
        self.analytics_courses[bundle.report_id] = course_id
        self.analytics_learner_scopes[bundle.report_id] = learner_scope
        return bundle.model_copy(deep=True)

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        bundle = self.analytics.get(report_id)
        return None if bundle is None else bundle.model_copy(deep=True)

    def get_scoped_analytics(
        self,
        report_id: str,
        *,
        course_id: str,
        class_id: str,
    ) -> TeacherAnalyticsBundle | None:
        bundle = self.analytics.get(report_id)
        if (
            bundle is None
            or self.analytics_courses.get(report_id) != course_id
            or bundle.class_report.class_id != class_id
        ):
            return None
        return bundle.model_copy(deep=True)

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        candidates = [
            bundle
            for report_id, bundle in self.analytics.items()
            if self.analytics_courses.get(report_id) == course_id
            and bundle.class_report.class_id == class_id
            and (
                learner_id is None
                or learner_id
                in self.analytics_learner_scopes.get(report_id, ())
            )
        ]
        if not candidates:
            return None
        latest = max(
            candidates,
            key=lambda bundle: (bundle.generated_at, bundle.report_id),
        )
        return latest.model_copy(deep=True)

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
