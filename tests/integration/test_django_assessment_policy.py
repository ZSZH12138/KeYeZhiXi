from __future__ import annotations

from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from tests.integration._django_web_support import (
    FakeCoordinator,
    FakeWebRuntime,
    make_user,
)
from tests.integration.test_django_student_qa import _active_release
from tests.integration.test_web_workflow_persistence import NOW


pytestmark = pytest.mark.django_db


class _PolicyCoordinator(FakeCoordinator):
    def __init__(self, actor_id: str) -> None:
        super().__init__(actor_id)
        self.started_item_count: int | None = None
        self.task: TaskPlan | None = None
        self.submitted_index_ref: EvidenceIndexRef | None = None

    def start_assessment(self, **kwargs):
        bundle = kwargs["knowledge_bundle"]
        task = TaskPlan(
            task_id="policy-task-1",
            task_type=kwargs["task_type_hint"],
            course_id=kwargs["course_id"],
            class_id=kwargs["class_id"],
            learner_id=kwargs["learner_id"],
            session_id=kwargs["session_id"],
            blueprint_id=bundle.blueprints[0].blueprint_id,
            knowledge_bundle_id=bundle.knowledge_bundle_id,
            course_package_id=bundle.course_package_id,
            workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
            next_module="M8",
            created_at=NOW,
        )
        self.task = task
        paper = PaperGenerator().generate(
            task,
            bundle,
            None,
            None,
            kwargs["selection_context"],
        )
        self.paper = paper
        self.started_item_count = len(paper.all_items())
        return {"task_plan": task, "assessment_paper": paper}

    def get_pending_assessment(self, **kwargs):
        pending = super().get_pending_assessment(**kwargs)
        if self.task is not None:
            pending = {**pending, "task_plan": self.task.model_copy(deep=True)}
        return pending

    def submit_assessment(self, **kwargs):
        self.submitted_index_ref = kwargs["index_ref"].model_copy(deep=True)
        return super().submit_assessment(**kwargs)


class _ReleaseIndexBuilder:
    def __init__(self) -> None:
        self.package = None

    def build_index(self, package):
        self.package = package.model_copy(deep=True)
        index_id = f"{package.course_package_id}_lexical_index"
        return EvidenceIndexRef(
            index_id=index_id,
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            index_version=package.package_version,
            storage_ref=f"lexical:{index_id}",
            backend="lexical",
            embedding_model_id=None,
            source_count=len(package.source_documents),
            chunk_count=len(package.content_chunks),
            built_at=package.imported_at,
            checksum="d" * 64,
            status="ready",
        )


def _post_start(client: Client, task_type: str):
    url = reverse(
        "student-start",
        kwargs={"course_id": "course_1", "class_id": "class_1"},
    )
    page = client.get(url)
    return client.post(
        url,
        {
            "task_type_hint": task_type,
            "flow_token": page.context["form"].initial["flow_token"],
        },
    )


def test_diagnostic_start_uses_active_teacher_release_and_draws_twenty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = make_user(
        actor_id="pseudonym_policy_student",
        role="student",
        permissions=("start_assessment",),
    )
    _active_release(student, example_count=25)
    coordinator = _PolicyCoordinator(student.actor_id)
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path),
    )
    client = Client()
    client.force_login(student)

    response = _post_start(client, "diagnostic")

    assert response.status_code == 302
    assert coordinator.started_item_count == 20


def test_generated_choice_question_renders_teacher_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE", "1")
    student = make_user(
        actor_id="pseudonym_policy_choice",
        role="student",
        permissions=("start_assessment", "submit_assessment"),
    )
    _active_release(
        student,
        example_count=1,
        with_choice_options=True,
    )
    coordinator = _PolicyCoordinator(student.actor_id)
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path),
    )
    client = Client()
    client.force_login(student)

    started = _post_start(client, "diagnostic")
    assert started.status_code == 302

    page = client.get(started["Location"])
    content = page.content.decode("utf-8")

    assert page.status_code == 200, content
    assert 'type="radio"' in content
    assert "A. 面向连接并保证可靠传输" in content
    assert "B. 无连接并保留应用报文边界" in content
    assert "C. 建立连接后才能发送数据" in content
    assert "D. 只支持字节流传输" in content


def test_submit_uses_evidence_index_bound_to_the_frozen_teacher_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = make_user(
        actor_id="pseudonym_policy_release_submit",
        role="student",
        permissions=("start_assessment", "submit_assessment"),
    )
    _active_release(student, example_count=1, with_choice_options=True)
    coordinator = _PolicyCoordinator(student.actor_id)
    web_runtime = FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path)
    index_builder = _ReleaseIndexBuilder()
    web_runtime.container.m2_service = index_builder
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client()
    client.force_login(student)

    started = _post_start(client, "diagnostic")
    paper_page = client.get(started["Location"])
    answer_name = next(iter(paper_page.context["form"].fields))
    submitted = client.post(
        paper_page.context["submit_url"],
        {
            answer_name: "B",
            "flow_token": paper_page.context["flow"],
        },
    )

    assert submitted.status_code == 302
    assert coordinator.task is not None
    assert coordinator.submitted_index_ref is not None
    assert (
        coordinator.submitted_index_ref.course_package_id
        == coordinator.task.course_package_id
    )
    assert index_builder.package is not None
    assert [source.file_name for source in index_builder.package.source_documents] == [
        "chapter.txt"
    ]
    assert all(
        chunk.chunk_id.startswith("chunk_") and len(chunk.chunk_id) == 70
        for chunk in index_builder.package.content_chunks
    )


def test_start_fails_clearly_when_teacher_has_no_published_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = make_user(
        actor_id="pseudonym_policy_empty",
        role="student",
        permissions=("start_assessment",),
    )
    coordinator = _PolicyCoordinator(student.actor_id)
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path),
    )
    client = Client()
    client.force_login(student)

    response = _post_start(client, "diagnostic")

    assert response.status_code == 400
    assert "教师尚未上传并发布可用题目" in response.content.decode()
    assert coordinator.started_item_count is None
