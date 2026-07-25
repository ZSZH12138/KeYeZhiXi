from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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

from course_insight.contracts.platform import AssessmentSubmission  # noqa: E402
from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from course_insight.modules.m0_platform.django_app.forms.assessment import (  # noqa: E402
    AssessmentSubmissionForm,
)
from tests.integration._django_web_support import (  # noqa: E402
    FakeCoordinator,
    FakeWebRuntime,
    make_user,
)


pytestmark = pytest.mark.django_db
STUDENT_PERMISSIONS = (
    "start_assessment",
    "submit_assessment",
    "view_own_result",
    "view_own_feedback",
)


def _csrf(client: Client) -> str:
    return client.cookies["csrftoken"].value


def test_student_flow_uses_exact_scope_existing_contracts_csrf_and_prg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(
        actor_id="pseudonym_student_001",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    coordinator = FakeCoordinator(user.actor_id)
    web_runtime = FakeWebRuntime(
        coordinator=coordinator,
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    start_url = reverse(
        "student-start",
        kwargs={"course_id": "course_1", "class_id": "class_1"},
    )
    start_page = client.get(start_url)
    assert start_page.status_code == 200
    flow = start_page.context["form"].initial["flow_token"]

    denied = client.post(
        start_url,
        {
            "student_text": "Create a governed practice assessment.",
            "task_type_hint": "practice",
            "flow_token": flow,
        },
    )
    assert denied.status_code == 403

    started = client.post(
        start_url,
        {
            "student_text": "Create a governed practice assessment.",
            "task_type_hint": "practice",
            "flow_token": flow,
            "csrfmiddlewaretoken": _csrf(client),
        },
    )
    assert started.status_code == 302
    assert "/assessments/paper_1/" in started["Location"]

    paper_page = client.get(started["Location"])
    assert paper_page.status_code == 200
    paper_flow = parse_qs(
        urlsplit(started["Location"]).query
    )["flow"][0]
    answer_name = AssessmentSubmissionForm.answer_field_name(
        "instance_1"
    )
    submit_url = reverse(
        "student-submit",
        kwargs={
            "course_id": "course_1",
            "class_id": "class_1",
            "paper_id": "paper_1",
        },
    )
    submitted = client.post(
        submit_url,
        {
            answer_name: "governed response",
            "flow_token": paper_flow,
            "csrfmiddlewaretoken": _csrf(client),
        },
    )
    assert submitted.status_code == 302
    assert "/results/paper_1/" in submitted["Location"]
    assert type(coordinator.submission) is AssessmentSubmission
    assert coordinator.submission.learner_id == user.actor_id
    assert coordinator.submission.paper_id == "paper_1"

    result_page = client.get(submitted["Location"])
    assert result_page.status_code == 200
    content = result_page.content.decode("utf-8")
    assert "1.0 / 1.0" in content
    assert "Review the cited course evidence." in content

    replay = client.post(
        submit_url,
        {
            answer_name: "changed but ignored after completion",
            "flow_token": paper_flow,
            "csrfmiddlewaretoken": _csrf(client),
        },
    )
    assert replay.status_code == 302
    assert "/results/paper_1/" in replay["Location"]


def test_student_cannot_cross_class_or_reuse_another_actor_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = make_user(
        actor_id="pseudonym_student_owner",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    other = make_user(
        actor_id="pseudonym_student_other",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    coordinator = FakeCoordinator(owner.actor_id)
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=coordinator,
            runtime_dir=tmp_path,
        ),
    )
    client = Client()
    client.force_login(owner)
    start = client.get(
        reverse(
            "student-start",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        )
    )
    flow = start.context["form"].initial["flow_token"]
    started = client.post(
        start.request["PATH_INFO"],
        {
            "student_text": "practice",
            "task_type_hint": "practice",
            "flow_token": flow,
        },
    )
    paper_url = started["Location"]

    unauthorized = client.get(
        reverse(
            "student-start",
            kwargs={"course_id": "course_1", "class_id": "class_2"},
        )
    )
    assert unauthorized.status_code == 403

    client.force_login(other)
    stolen = client.get(paper_url)
    assert stolen.status_code == 400
