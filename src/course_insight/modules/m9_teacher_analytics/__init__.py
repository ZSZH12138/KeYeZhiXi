"""M9 teacher-analytics module."""

from course_insight.modules.m9_teacher_analytics.repository import M9Repository
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from course_insight.modules.m9_teacher_analytics.stubs import (
    M9TeacherAnalyticsServiceStub,
)

__all__ = [
    "M9Repository",
    "M9TeacherAnalyticsService",
    "M9TeacherAnalyticsServiceStub",
]
