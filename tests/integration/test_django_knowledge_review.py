from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import django
import pytest
from django.apps import apps
from django.test import Client
from django.urls import reverse

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "course_insight.web_project.settings")
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from course_insight.modules.m3_knowledge_bundle.teacher_review import (  # noqa: E402
    InMemoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService  # noqa: E402
from tests.integration._django_web_support import make_user  # noqa: E402


pytestmark = pytest.mark.django_db


class _Coordinator:
    def __init__(self) -> None:
        self.repo = InMemoryTeacherReviewRepository()
        self.workflow = TeacherReviewWorkflow(self.repo)
        self.service = M3KnowledgeBundleService(
            repository=SimpleNamespace(),
            schema_validator=SimpleNamespace(),
            review_workflow=self.workflow,
        )
        self.review = self.workflow.create_draft(
            review_id="review-1",
            subject_id="package-1",
            input_checksum="a" * 64,
            validation_report_ref="validation-1",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    def get_knowledge_review(self, *, review_id: str, **_: object):
        return self.repo.get(review_id)

    def submit_knowledge_review(self, review_id, reviewer_pseudonym, reason, expected_version, now):
        return self.workflow.submit(review_id, reviewer_pseudonym, reason, expected_version, now)


class _Runtime:
    def __init__(self, coordinator: _Coordinator, runtime_dir: Path) -> None:
        self.container = SimpleNamespace(
            coordinator=coordinator,
            settings=SimpleNamespace(web=SimpleNamespace(session_timeout_seconds=3600)),
        )
        self.courses = {
            "course-1": SimpleNamespace(
                course_context=SimpleNamespace(
                    course_package=SimpleNamespace(course_package_id="package-1"),
                )
            )
        }

    def require_course(self, course_id: str):
        return self.courses[course_id]


def test_teacher_can_submit_m3_knowledge_review_with_signed_versioned_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_knowledge_1",
        role="teacher",
        permissions=("view_class_analytics", "review_score"),
        course_id="course-1",
        class_id="class-1",
    )
    coordinator = _Coordinator()
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: _Runtime(coordinator, tmp_path))
    client = Client()
    client.force_login(teacher)

    url = reverse(
        "teacher-knowledge-review",
        kwargs={
            "course_id": "course-1",
            "class_id": "class-1",
            "review_id": "review-1",
        },
    )
    page = client.get(url)
    assert page.status_code == 200
    flow = page.context["flow"]

    submitted = client.post(
        url,
        {"action": "submit", "reason": "材料已核对", "flow_token": flow},
    )
    assert submitted.status_code == 302
    assert coordinator.repo.get("review-1").state == "submitted"


def test_teacher_can_find_knowledge_review_from_basic_teacher_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    teacher = make_user(
        actor_id="pseudonym_teacher_knowledge_2",
        role="teacher",
        permissions=("view_class_analytics", "review_score"),
        course_id="course-1",
        class_id="class-1",
    )
    coordinator = _Coordinator()
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: _Runtime(coordinator, tmp_path))
    client = Client()
    client.force_login(teacher)
    response = client.get(
        reverse("teacher-knowledge-review-lookup"),
        {"course_id": "course-1", "class_id": "class-1", "review_id": "review-1"},
    )
    assert response.status_code == 302
    assert response["Location"].endswith("/knowledge-reviews/review-1/")
