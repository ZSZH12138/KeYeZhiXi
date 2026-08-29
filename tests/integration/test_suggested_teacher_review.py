from __future__ import annotations

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from course_insight.modules.m0_platform.django_app import runtime

from course_insight.modules.m0_platform.django_app.models import (
    SuggestedTeacherReviewCase,
    SuggestedTeacherReviewItem,
)
from course_insight.modules.m0_platform.django_app.suggested_review import (
    sync_suggested_review_case,
)
from tests.factories.m5_m8 import make_paper, make_scoring_bundle, make_task_plan
from tests.integration._django_web_support import make_user
from tests.integration._django_web_support import FakeWebRuntime
from tests.integration.test_django_student_qa import _active_release
from tests.integration.test_teacher_question_review import (
    _QuestionReviewCoordinator,
)


pytestmark = pytest.mark.django_db


def _pending_scoring(paper):
    scoring = make_scoring_bundle(paper, score=0.0)
    audit = scoring.score_audit_records[0].model_copy(
        update={
            "confidence": 0.4,
            "scoring_method": "local_model",
            "review_status": "pending",
            "review_reason": ["teacher_review_required", "low_confidence"],
        },
        deep=True,
    )
    return scoring.model_copy(update={"score_audit_records": [audit]}, deep=True)


def test_profile_assessment_creates_one_idempotent_suggested_review_case() -> None:
    learner = make_user(
        actor_id="pseudonym_suggested_review_student",
        role="student",
        permissions=("view_own_result",),
    )
    paper = make_paper().model_copy(update={"learner_id": learner.actor_id})
    scoring = _pending_scoring(paper).model_copy(
        update={"learner_id": learner.actor_id}, deep=True
    )
    task = make_task_plan(learner_id=learner.actor_id).model_copy(
        update={"task_type": "diagnostic"}
    )

    first = sync_suggested_review_case(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    second = sync_suggested_review_case(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )

    assert first is not None and second is not None
    assert first.pk == second.pk
    assert SuggestedTeacherReviewCase.objects.count() == 1
    item = SuggestedTeacherReviewItem.objects.get()
    assert item.item_instance_id == paper.all_items()[0].item_instance_id
    assert item.confidence == Decimal("0.400")


def test_non_profile_assessment_never_enters_suggested_review() -> None:
    learner = make_user(
        actor_id="pseudonym_untracked_review_student",
        role="student",
        permissions=("view_own_result",),
    )
    paper = make_paper().model_copy(update={"learner_id": learner.actor_id})
    scoring = _pending_scoring(paper).model_copy(
        update={"learner_id": learner.actor_id}, deep=True
    )
    task = make_task_plan(learner_id=learner.actor_id).model_copy(
        update={"task_type": "practice"}
    )

    result = sync_suggested_review_case(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )

    assert result is None
    assert not SuggestedTeacherReviewCase.objects.exists()


def test_teacher_can_open_grouped_suggested_review_queue(
    tmp_path,
    monkeypatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_suggested_review_teacher",
        role="teacher",
        permissions=(
            "view_class_analytics",
            "view_student_report",
            "review_score",
        ),
    )
    learner = make_user(
        actor_id="pseudonym_suggested_queue_student",
        role="student",
        permissions=("view_own_result",),
    )
    source = _active_release(teacher, example_count=1)
    release = source.versions.first().concept_references.first().concept.release
    coordinator = _QuestionReviewCoordinator(
        learner.actor_id,
        release_id=str(release.pk),
    )
    coordinator.scoring = _pending_scoring(coordinator.paper).model_copy(
        update={"learner_id": learner.actor_id}, deep=True
    )
    case = sync_suggested_review_case(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=coordinator.task,
        paper=coordinator.paper,
        scoring=coordinator.scoring,
    )
    web_runtime = FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path)
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client()
    client.force_login(teacher)

    queue = client.get(
        reverse(
            "teacher-suggested-review-list",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        )
    )
    detail = client.get(
        reverse(
            "teacher-suggested-review-detail",
            kwargs={
                "course_id": "course_1",
                "class_id": "class_1",
                "case_id": case.pk,
            },
        )
    )

    assert queue.status_code == 200
    assert learner.actor_id in queue.content.decode("utf-8")
    content = detail.content.decode("utf-8")
    assert detail.status_code == 200, content
    assert coordinator.paper.paper_id in content
    assert "改判本题" in content

    review = detail.context["question_reviews"][0]
    expected_detail_url = reverse(
        "teacher-suggested-review-detail",
        kwargs={
            "course_id": "course_1",
            "class_id": "class_1",
            "case_id": case.pk,
        },
    )
    item_rescore_url = reverse(
        "teacher-item-rescore",
        kwargs={
            "course_id": "course_1",
            "class_id": "class_1",
            "paper_id": coordinator.paper.paper_id,
            "item_instance_id": (
                coordinator.paper.all_items()[0].item_instance_id
            ),
        },
    )
    assert review.action_url == (
        f"{item_rescore_url}?suggested_case_id={case.pk}"
    )
    submitted = client.post(
        review.action_url,
        {
            "score": "1",
            "teacher_note": "建议复核后确认正确。",
            "flow_token": review.form.initial["flow_token"],
        },
    )

    assert submitted.status_code == 302
    assert submitted.url == expected_detail_url
    assert coordinator.review_student_evidence == "学生填写的答案"
    assert coordinator.review_index_ref.course_package_id == (
        coordinator.task.course_package_id
    )
    completed = client.get(submitted.url)
    completed_content = completed.content.decode("utf-8")
    assert completed.status_code == 200, completed_content
    assert "本试卷的建议复核已经完成" in completed_content
