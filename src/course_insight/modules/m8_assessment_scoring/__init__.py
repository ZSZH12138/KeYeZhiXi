"""M8 assessment-and-scoring module."""

from course_insight.modules.m8_assessment_scoring.repository import M8Repository
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from course_insight.modules.m8_assessment_scoring.stubs import M8AssessmentServiceStub

__all__ = ["M8AssessmentService", "M8AssessmentServiceStub", "M8Repository"]
