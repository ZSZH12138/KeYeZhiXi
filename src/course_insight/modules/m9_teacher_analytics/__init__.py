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
    "M9Repository",
    "M9InvocationFailure",
    "M9NarrativeOutcome",
    "M9NarrativePolicy",
    "M9TeacherAnalyticsService",
    "M9TeacherAnalyticsServiceStub",
]
