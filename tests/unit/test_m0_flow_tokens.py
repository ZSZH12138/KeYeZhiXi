from __future__ import annotations

import os

import django
from django.apps import apps


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app.flow_tokens import (  # noqa: E402
    flow_issued_at,
    issue_flow_token,
    stable_flow_identifier,
    verify_flow_token,
)
from course_insight.modules.m0_platform.django_app.forms.assessment import (  # noqa: E402
    AssessmentSubmissionForm,
)
from course_insight.modules.m0_platform.django_app.forms.review import (  # noqa: E402
    TeacherReviewForm,
)
from tests.unit.test_m0_forms import _audit, _paper  # noqa: E402


def test_signed_flow_token_returns_one_stable_utc_submission_time() -> None:
    token = issue_flow_token(
        purpose="assessment_submit",
        actor_id="pseudonym_student_001",
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
    )
    first = verify_flow_token(
        token,
        purpose="assessment_submit",
        actor_id="pseudonym_student_001",
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
        max_age_seconds=3600,
    )
    second = verify_flow_token(
        token,
        purpose="assessment_submit",
        actor_id="pseudonym_student_001",
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
        max_age_seconds=3600,
    )

    assert flow_issued_at(first) == flow_issued_at(second)
    assert flow_issued_at(first).utcoffset().total_seconds() == 0


def test_retry_builds_identical_assessment_submission_checksum() -> None:
    paper = _paper()
    token = issue_flow_token(
        purpose="assessment_submit",
        actor_id=paper.learner_id,
        course_id="course_1",
        class_id="class_1",
        paper_id=paper.paper_id,
    )
    verified = verify_flow_token(
        token,
        purpose="assessment_submit",
        actor_id=paper.learner_id,
        course_id="course_1",
        class_id="class_1",
        paper_id=paper.paper_id,
        max_age_seconds=3600,
    )
    data = {
        AssessmentSubmissionForm.answer_field_name(
            "objective_bool"
        ): "true",
        AssessmentSubmissionForm.answer_field_name(
            "subjective_text"
        ): "governed answer",
    }

    submissions = [
        AssessmentSubmissionForm(
            paper=paper,
            learner_id=paper.learner_id,
            data=data,
        ).to_submission(
            submission_id=stable_flow_identifier("submission", token),
            attempt_id=stable_flow_identifier("attempt", token),
            submitted_at=flow_issued_at(verified),
        )
        for _ in range(2)
    ]

    assert submissions[0].submitted_at == submissions[1].submitted_at
    assert (
        submissions[0].content_checksum()
        == submissions[1].content_checksum()
    )


def test_retry_builds_identical_teacher_review_checksum() -> None:
    audit = _audit()
    token = issue_flow_token(
        purpose="teacher_review",
        actor_id="pseudonym_teacher_001",
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
        audit_id=audit.audit_id,
        audit_version=audit.audit_version,
    )
    verified = verify_flow_token(
        token,
        purpose="teacher_review",
        actor_id="pseudonym_teacher_001",
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
        audit_id=audit.audit_id,
        audit_version=audit.audit_version,
        max_age_seconds=3600,
    )
    data = {
        "decision": "confirm",
        "final_total_score": str(audit.total_score),
        "teacher_comment": "Confirmed against governed evidence.",
    }
    reviews = [
        TeacherReviewForm(
            audit=audit,
            reviewer_id="pseudonym_teacher_001",
            criterion_caps={"accuracy": 2.0, "reasoning": 2.0},
            data=data,
        ).to_submission(
            submission_id=stable_flow_identifier("review", token),
            submitted_at=flow_issued_at(verified),
        )
        for _ in range(2)
    ]

    assert reviews[0].submitted_at == reviews[1].submitted_at
    assert reviews[0].content_checksum() == reviews[1].content_checksum()
