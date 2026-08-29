from __future__ import annotations

import os
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

from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from course_insight.modules.m0_platform.django_app.flow_tokens import (  # noqa: E402
    issue_flow_token,
)
from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    AssessmentProjectionReceipt,
    CourseClassWorkspace,
)
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (  # noqa: E402
    save_scoped_deepseek_settings,
)
from tests.factories.m5_m8 import (  # noqa: E402
    make_paper,
    make_scoring_bundle,
    make_task_plan,
)
from tests.integration._django_web_support import (  # noqa: E402
    FakeCoordinator,
    FakeWebRuntime,
    feedback_for,
    make_user,
)
from tests.integration.test_django_student_qa import (  # noqa: E402
    _FakeAdapter,
    _active_release,
)


pytestmark = pytest.mark.django_db

STUDENT_PERMISSIONS = (
    "start_assessment",
    "view_own_result",
    "view_own_feedback",
    "ask_course_question",
)


class _FinalAssessmentCoordinator(FakeCoordinator):
    def __init__(self, actor_id: str, *, release_id: str) -> None:
        super().__init__(actor_id)
        candidate = make_paper(
            item_id="q-1",
            concept_ids=["concept-1"],
        ).model_copy(update={"learner_id": actor_id})
        self.paper = candidate.model_copy(
            update={"immutable_checksum": candidate.freeze()}
        )
        self.scoring = make_scoring_bundle(self.paper, score=0.0)
        self.feedback = feedback_for(actor_id)
        self.task = make_task_plan(learner_id=actor_id).model_copy(
            update={
                "task_type": "diagnostic",
                "knowledge_bundle_id": release_id,
                "course_package_id": f"course-release-{release_id}",
            }
        )

    def get_student_assessment(self, **kwargs):
        if (
            kwargs["learner_id"] != self.paper.learner_id
            or kwargs["paper_id"] != self.paper.paper_id
        ):
            return super().get_student_assessment(**kwargs)
        return {
            "task_plan": self.task.model_copy(deep=True),
            "assessment_paper": self.paper.model_copy(deep=True),
            "scoring_result": self.scoring.model_copy(deep=True),
            "feedback": self.feedback.model_copy(deep=True),
        }


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    coordinator: FakeCoordinator,
) -> None:
    monkeypatch.setenv("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE", "1")
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path),
    )


def test_start_page_lists_only_own_profile_affecting_finalized_papers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = make_user(
        actor_id="pseudonym_history_owner",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    other = make_user(
        actor_id="pseudonym_history_other",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_1",
    )
    other_workspace = CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_2",
    )
    AssessmentProjectionReceipt.objects.create(
        workspace=workspace,
        learner=student,
        attempt_id="attempt-profile",
        paper_id="paper_1",
        task_type="diagnostic",
        scoring_checksum="a" * 64,
        projection_payload={"instance_1": {"correct": False}},
    )
    AssessmentProjectionReceipt.objects.create(
        workspace=workspace,
        learner=other,
        attempt_id="attempt-other",
        paper_id="paper-other-hidden",
        task_type="diagnostic",
        scoring_checksum="c" * 64,
        projection_payload={"instance_1": {"correct": False}},
    )
    AssessmentProjectionReceipt.objects.create(
        workspace=other_workspace,
        learner=student,
        attempt_id="attempt-other-class",
        paper_id="paper-other-class-hidden",
        task_type="stage_assessment",
        scoring_checksum="d" * 64,
        projection_payload={"instance_1": {"correct": False}},
    )
    coordinator = FakeCoordinator(student.actor_id)
    coordinator.scoring = make_scoring_bundle(coordinator.paper, score=0.0)
    _runtime(monkeypatch, tmp_path, coordinator)
    client = Client()
    client.force_login(student)

    response = client.get(
        reverse(
            "student-start",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        )
    )

    content = response.content.decode("utf-8")
    assert response.status_code == 200
    assert "画像测评记录" in content
    assert "paper_1" in content
    assert "0.0 / 1.0" in content
    assert "错题 1 道" in content
    assert "paper-practice-hidden" not in content
    assert "paper-other-hidden" not in content
    assert "paper-other-class-hidden" not in content
    history = response.context["assessment_history"]
    assert len(history) == 1
    assert history[0].result_url.startswith(
        "/student/courses/course_1/classes/class_1/results/paper_1/"
    )


def test_result_page_shows_choice_options_answer_sources_and_post_qa_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    student = make_user(
        actor_id="pseudonym_result_detail",
        role="student",
        permissions=STUDENT_PERMISSIONS,
    )
    source = _active_release(
        student,
        example_count=1,
        with_choice_options=True,
    )
    release = source.versions.first().concept_references.first().concept.release
    question = release.questions.get(question_id="q-1")
    question.payload = {**question.payload, "answers": ["B"]}
    question.payload.pop("accepted_answers", None)
    question.save(update_fields=("payload",))
    coordinator = _FinalAssessmentCoordinator(
        student.actor_id,
        release_id=str(release.pk),
    )
    _runtime(monkeypatch, tmp_path, coordinator)
    client = Client()
    client.force_login(student)
    flow = issue_flow_token(
        purpose="student_result",
        actor_id=student.actor_id,
        course_id="course_1",
        class_id="class_1",
        paper_id="paper_1",
    )

    response = client.get(
        reverse(
            "student-result",
            kwargs={
                "course_id": "course_1",
                "class_id": "class_1",
                "paper_id": "paper_1",
            },
        ),
        {"flow": flow},
    )

    content = response.content.decode("utf-8")
    assert response.status_code == 200, content
    assert "原题目" in content
    assert "Question" in content
    assert "A. 面向连接并保证可靠传输" in content
    assert "B. 无连接并保留应用报文边界" in content
    assert "题目正确答案" in content
    assert "学生答案" in content
    assert "学生得分" in content
    assert "教师备注" in content
    assert ">B<" in content
    assert "拥塞控制" in content
    assert "chapter.txt" in content
    assert 'method="post"' in content
    assert 'name="question_ref"' in content
    assert "答疑分析本题" in content
    assert reverse(
        "student-qa",
        kwargs={"course_id": "course_1", "class_id": "class_1"},
    ) in content

    class _CapturingAdapter(_FakeAdapter):
        question = ""

        def answer(self, question, concepts, context_loader, **kwargs):
            self.question = question
            return super().answer(
                question,
                concepts,
                context_loader,
                **kwargs,
            )

    adapter = _CapturingAdapter()
    save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-student-result-handoff-test",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=student,
    )
    monkeypatch.setattr(
        "course_insight.modules.m0_platform.django_app.views.student_qa.build_student_qa_adapter",
        lambda *args: adapter,
    )
    qa_response = client.post(
        reverse(
            "student-qa",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {"question_ref": response.context["question_details"][0].qa_token},
    )

    assert qa_response.status_code == 200
    assert "参考答案：B" in qa_response.content.decode("utf-8")
    assert "题目：Question" in adapter.question
    assert "B. 无连接并保留应用报文边界" in adapter.question
    assert "正确答案：B" in adapter.question
    assert "涉及知识点：拥塞控制" in adapter.question
