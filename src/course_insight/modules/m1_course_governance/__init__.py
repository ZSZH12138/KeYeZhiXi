"""M1 course-governance module."""

from course_insight.modules.m1_course_governance.repository import M1Repository
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)
from course_insight.modules.m1_course_governance.stubs import (
    M1CourseGovernanceServiceStub,
)

__all__ = [
    "M1CourseGovernanceService",
    "M1CourseGovernanceServiceStub",
    "M1Repository",
]
