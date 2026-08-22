"""M9 teacher-analytics module."""

from course_insight.modules.m9_teacher_analytics.adapter import (
    DeepSeekM9NarrativeAdapter,
    GovernedM9NarrativeAdapter,
    M9InvocationFailure,
    M9NarrativeOutcome,
)
from course_insight.modules.m9_teacher_analytics.policy import (
    DEFAULT_M9_NARRATIVE_POLICY,
    M9NarrativePolicy,
)
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
    M9Repository,
    M9ReviewDecisionConflict,
)
from course_insight.modules.m9_teacher_analytics.narrative_evaluation import (
    M9_NARRATIVE_CANDIDATE_DEFAULT_ENABLED,
    NarrativeEvaluationCase,
    evaluate_narrative_cases,
)
from course_insight.modules.m9_teacher_analytics.quality import (
    M9QualityGatePolicy,
    TechnicalQualityExpectation,
    evaluate_technical_quality_evidence,
)
from course_insight.modules.m9_teacher_analytics.review_sampling import (
    ReviewSamplingPolicy,
    build_review_queue,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from course_insight.modules.m9_teacher_analytics.stubs import (
    M9TeacherAnalyticsServiceStub,
)

__all__ = [
    "DEFAULT_M9_NARRATIVE_POLICY",
    "DeepSeekM9NarrativeAdapter",
    "GovernedM9NarrativeAdapter",
    "M9ModelAuditRecord",
    "M9_NARRATIVE_CANDIDATE_DEFAULT_ENABLED",
    "M9Repository",
    "M9ReviewDecisionConflict",
    "M9InvocationFailure",
    "M9NarrativeOutcome",
    "M9NarrativePolicy",
    "M9QualityGatePolicy",
    "M9TeacherAnalyticsService",
    "M9TeacherAnalyticsServiceStub",
    "NarrativeEvaluationCase",
    "ReviewSamplingPolicy",
    "TechnicalQualityExpectation",
    "build_review_queue",
    "evaluate_technical_quality_evidence",
    "evaluate_narrative_cases",
]
