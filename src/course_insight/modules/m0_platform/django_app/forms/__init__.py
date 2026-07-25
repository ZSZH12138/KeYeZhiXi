"""Django forms that construct the existing M0 submission contracts."""

from course_insight.modules.m0_platform.django_app.forms.assessment import (
    AssessmentSubmissionForm,
)
from course_insight.modules.m0_platform.django_app.forms.review import (
    TeacherReviewForm,
)

__all__ = ["AssessmentSubmissionForm", "TeacherReviewForm"]
