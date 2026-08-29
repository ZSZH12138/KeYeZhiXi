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
    monkeypatch.setenv("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE", "1")
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
    assert "student_text" not in start_page.context["form"].fields
    assert "<textarea" not in start_page.content.decode("utf-8")
    flow = start_page.context["form"].initial["flow_token"]

    denied = client.post(
        start_url,
        {
            "task_type_hint": "practice",
            "flow_token": flow,
        },
    )
    assert denied.status_code == 403

    started = client.post(
        start_url,
        {
            "task_type_hint": "practice",
            "flow_token": flow,
            "csrfmiddlewaretoken": _csrf(client),
        },
    )
    assert started.status_code == 302
    assert "/assessments/paper_1/" in started["Location"]
    assert coordinator.start_request is not None
    assert coordinator.start_request["student_text"] == "请开始随心练习"

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
    assert "成绩正在复核中" in content
    assert "返回掌握画像（首页）" in content
    assert "1.0 / 1.0" not in content
    assert "Review the cited course evidence." not in content
    assert "安全反馈" not in content
    assert "暂不公布最终得分" in content

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
    monkeypatch.setenv("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE", "1")
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


def test_student_authorized_for_an_unpublished_course_gets_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(
        actor_id="pseudonym_student_unpublished_course",
        role="student",
        permissions=STUDENT_PERMISSIONS,
        course_id="course_other",
        class_id="class_99",
    )
    web_runtime = runtime.WebRuntime(
        container=object(),  # type: ignore[arg-type]
        courses={},
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client()
    client.force_login(user)

    response = client.get(
        reverse(
            "student-start",
            kwargs={"course_id": "course_other", "class_id": "class_99"},
        )
    )

    content = response.content.decode("utf-8")
    assert response.status_code == 404, content
    assert "请求的资源不存在" in content
    assert "RUNTIME_CONTEXT" not in content


def test_legacy_correction_json_is_not_used_as_student_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE", "1")
    import json

    user = make_user(
        actor_id="pseudonym_student_001",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    coordinator = FakeCoordinator(user.actor_id)
    web_runtime = FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path)
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    (tmp_path / "correction_records.json").write_text(
        json.dumps(
            {
                "paper_source": {
                    "items": {
                        "lost_instance": {
                            "follow_up_paper_id": "paper_1",
                            "follow_up_item_id": "item_1",
                            "source_item_id": "item_1",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    client = Client()
    client.force_login(user)
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
            "task_type_hint": "practice",
            "flow_token": flow,
        },
    )
    paper_flow = parse_qs(urlsplit(started["Location"]).query)["flow"][0]
    answer_name = AssessmentSubmissionForm.answer_field_name("instance_1")
    submitted = client.post(
        reverse(
            "student-submit",
            kwargs={
                "course_id": "course_1",
                "class_id": "class_1",
                "paper_id": "paper_1",
            },
        ),
        {
            answer_name: "governed response",
            "flow_token": paper_flow,
        },
    )
    result_page = client.get(submitted["Location"])

    assert result_page.status_code == 200
    content = result_page.content.decode("utf-8")
    assert "返回原卷订正页" not in content
    assert "返回掌握画像（首页）" in content
    assert "paper_source" not in content
    assert "#lost-lost_instance" not in content
