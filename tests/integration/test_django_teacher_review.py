from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import django
import pytest
from django.apps import apps
from django.test import Client
from django.urls import reverse


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.contracts.errors import DomainError  # noqa: E402
from course_insight.contracts.platform import (  # noqa: E402
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from course_insight.modules.m0_platform.django_app.flow_tokens import (  # noqa: E402
    issue_flow_token,
)
from tests.integration._django_web_support import (  # noqa: E402
    FakeCoordinator,
    FakeWebRuntime,
    make_user,
)


pytestmark = pytest.mark.django_db
TEACHER_PERMISSIONS = (
    "view_class_analytics",
    "view_student_report",
    "review_score",
)


def _review_url(*, class_id: str = "class_1") -> str:
    return reverse(
        "teacher-review",
        kwargs={
            "course_id": "course_1",
            "class_id": class_id,
            "paper_id": "paper_1",
            "audit_id": "audit_1",
        },
    )


def test_teacher_review_builds_existing_contract_and_uses_prg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_001",
        role="teacher",
        permissions=TEACHER_PERMISSIONS,
    )
    coordinator = FakeCoordinator("pseudonym_student_001")
    web_runtime = FakeWebRuntime(
        coordinator=coordinator,
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client(enforce_csrf_checks=True)
    client.force_login(teacher)

    page = client.get(_review_url())
    assert page.status_code == 200
    assert "governed response" in page.content.decode("utf-8")
    flow = page.context["flow"]
    csrf = client.cookies["csrftoken"].value

    denied = client.post(
        _review_url(),
        {
            "decision": "confirm",
            "final_total_score": "1.0",
            "teacher_comment": "Confirmed against governed evidence.",
            "flow_token": flow,
        },
    )
    assert denied.status_code == 403

    submitted = client.post(
        _review_url(),
        {
            "decision": "confirm",
            "final_total_score": "1.0",
            "teacher_comment": "Confirmed against governed evidence.",
            "flow_token": flow,
            "csrfmiddlewaretoken": csrf,
        },
    )
    assert submitted.status_code == 302
    assert "/reviews/paper_1/" in submitted["Location"]
    assert type(coordinator.review_submission) is TeacherReviewSubmission
    assert coordinator.review_submission.reviewer_id == teacher.actor_id
    assert coordinator.review_submission.audit_id == "audit_1"
    assert coordinator.review_submission.expected_audit_version == 1
    assert coordinator.review_submission.expected_audit_checksum == (
        coordinator.scoring.get_audit_record("audit_1").content_checksum()
    )


def test_teacher_cannot_cross_class_and_stale_version_returns_409(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_scope",
        role="teacher",
        permissions=TEACHER_PERMISSIONS,
    )
    coordinator = FakeCoordinator("pseudonym_student_001")
    web_runtime = FakeWebRuntime(
        coordinator=coordinator,
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client()
    client.force_login(teacher)

    assert client.get(_review_url(class_id="class_2")).status_code == 403

    stale = issue_flow_token(
        purpose="teacher_review",
        actor_id=teacher.actor_id,
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
        audit_id="audit_1",
        audit_version=2,
    )
    conflict = client.post(
        _review_url(),
        {
            "decision": "confirm",
            "final_total_score": "1.0",
            "teacher_comment": "Stale review.",
            "flow_token": stale,
        },
    )
    assert conflict.status_code == 409
    assert "changed before" not in conflict.content.decode("utf-8")


def test_domain_not_found_is_mapped_without_exception_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_error",
        role="teacher",
        permissions=TEACHER_PERMISSIONS,
    )
    coordinator = FakeCoordinator("pseudonym_student_001")
    coordinator.raise_context = DomainError(
        code="ASSESSMENT_NOT_FOUND",
        module="application",
        message="secret C:/host/path database detail",
        details={"path": "C:/host/path", "sql": "SELECT secret"},
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=coordinator,
            runtime_dir=tmp_path,
        ),
    )
    client = Client()
    client.force_login(teacher)
    response = client.get(
        reverse(
            "teacher-review-context",
            kwargs={
                "course_id": "course_1",
                "class_id": "class_1",
                "paper_id": "paper_1",
            },
        )
    )

    assert response.status_code == 404
    body = response.content.decode("utf-8")
    assert "C:/host/path" not in body
    assert "SELECT secret" not in body


def test_teacher_context_requires_both_class_and_student_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_partial_permission",
        role="teacher",
        permissions=("view_student_report",),
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=FakeCoordinator("pseudonym_student_001"),
            runtime_dir=tmp_path,
        ),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-review-context",
            kwargs={
                "course_id": "course_1",
                "class_id": "class_1",
                "paper_id": "paper_1",
            },
        )
    )

    assert response.status_code == 403


@pytest.mark.parametrize("method", ("get", "post"))
def test_teacher_review_requires_student_report_permission(
    method: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id=f"pseudonym_teacher_review_without_report_{method}",
        role="teacher",
        permissions=("view_class_analytics", "review_score"),
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=FakeCoordinator("pseudonym_student_001"),
            runtime_dir=tmp_path,
        ),
    )
    client = Client()
    client.force_login(teacher)

    response = getattr(client, method)(_review_url())

    assert response.status_code == 403


def test_teacher_can_request_bound_model_rescore_without_auto_accept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_rescore",
        role="teacher",
        permissions=TEACHER_PERMISSIONS,
    )
    coordinator = FakeCoordinator("pseudonym_student_001")
    rejected = coordinator.scoring.get_audit_record("audit_1").model_copy(
        update={
            "review_status": "rejected_pending_rescore",
            "review_reason": ["teacher_rejected_score"],
        },
        deep=True,
    )
    coordinator.scoring = coordinator.scoring.model_copy(
        update={"score_audit_records": [rejected]},
        deep=True,
    )
    coordinator.submission = AssessmentSubmission(
        submission_id="submission_1",
        attempt_id=coordinator.scoring.attempt_id,
        paper_id="paper_1",
        learner_id="pseudonym_student_001",
        answers={"instance_1": "governed response"},
        submitted_at=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=coordinator,
            runtime_dir=tmp_path,
        ),
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(teacher)
    context_url = reverse(
        "teacher-review-context",
        kwargs={
            "course_id": "course_1",
            "class_id": "class_1",
            "paper_id": "paper_1",
        },
    )

    page = client.get(context_url)
    content = page.content.decode("utf-8")

    assert page.status_code == 200
    assert "用原作答请求模型重评" in content
    assert "不会自动接受" in content
    flow = page.context["rescore_flow"]
    posted = client.post(
        page.context["rescore_url"],
        {
            "flow_token": flow,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )

    assert posted.status_code == 302
    assert "/reviews/paper_1/" in posted["Location"]
    assert coordinator.rescore_submission is not None
    assert coordinator.rescore_submission.attempt_id == "attempt_1"
    assert coordinator.rescore_audit_id == "audit_1"
    assert coordinator.rescore_submission.answers == {
        "instance_1": "governed response",
    }
