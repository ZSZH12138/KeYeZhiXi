"""Django forms that construct the existing M0 submission contracts."""

from course_insight.modules.m0_platform.django_app.forms.assessment import (
    AssessmentSubmissionForm,
)
from course_insight.modules.m0_platform.django_app.forms.review import (
    TeacherReviewForm,
)
from course_insight.modules.m0_platform.django_app.forms.governance import (
    AccountDeletionConfirmationForm,
    ClassMemberForm,
    ManagedAccountCreationForm,
    OpenClassForm,
)

__all__ = [
    "AccountDeletionConfirmationForm",
    "AssessmentSubmissionForm",
    "ClassMemberForm",
    "ManagedAccountCreationForm",
    "OpenClassForm",
    "TeacherReviewForm",
]
