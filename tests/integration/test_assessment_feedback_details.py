from __future__ import annotations

import pytest

from course_insight.modules.m0_platform.django_app.assessment_feedback import (
    question_feedback_details,
)
from course_insight.modules.m0_platform.django_app.models import User
from tests.factories.m5_m8 import make_paper, make_scoring_bundle, make_task_plan
from tests.integration.test_django_student_qa import _active_release


pytestmark = pytest.mark.django_db


def test_feedback_contains_question_options_concepts_answer_and_active_source() -> None:
    actor_id = "pseudonym_feedback_detail"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    source = _active_release(user, example_count=1, with_choice_options=True)
    release = source.versions.first().concept_references.first().concept.release
    question = release.questions.get(question_id="q-1")
    question.payload = {
        "options": {
            "A": "面向连接并保证可靠传输",
            "B": "无连接并保留应用报文边界",
            "C": "建立连接后才能发送数据",
            "D": "只支持字节流传输",
        },
        "answers": ["B"],
    }
    question.save(update_fields=("payload",))
    task = make_task_plan(learner_id=actor_id).model_copy(
        update={
            "knowledge_bundle_id": str(release.pk),
            "course_package_id": f"course-release-{release.pk}",
        }
    )
    paper = make_paper(
        item_id="q-1",
        concept_ids=["concept-1"],
    ).model_copy(update={"learner_id": actor_id})

    scoring = make_scoring_bundle(paper, score=0.0)
    details = question_feedback_details(
        task=task,
        paper=paper,
        scoring=scoring,
        answers={paper.all_items()[0].item_instance_id: "学生选择 A"},
        teacher_notes={paper.all_items()[0].item_instance_id: "请复习报文边界。"},
    )

    assert len(details) == 1
    assert details[0].stem == "Question"
    assert details[0].options == (
        ("A", "面向连接并保证可靠传输"),
        ("B", "无连接并保留应用报文边界"),
        ("C", "建立连接后才能发送数据"),
        ("D", "只支持字节流传输"),
    )
    assert details[0].concept_names == ("拥塞控制",)
    assert details[0].correct_answer == "B"
    assert details[0].student_answer == "学生选择 A"
    assert details[0].student_score == 0.0
    assert details[0].teacher_note == "请复习报文边界。"
    assert details[0].requires_ai_assessment is False
    assert details[0].source_blocks[0].file_name == "chapter.txt"
    assert details[0].source_blocks[0].text == "拥塞控制用于避免网络过载。"
    assert "B. 无连接并保留应用报文边界" in details[0].qa_prompt
    assert "正确答案：B" in details[0].qa_prompt

    source.status = "deleted"
    source.save(update_fields=("status", "updated_at"))
    assert question_feedback_details(task=task, paper=paper)[0].source_blocks == ()


def test_fill_blank_feedback_shows_ai_reason_confidence_and_review_warning() -> None:
    actor_id = "pseudonym_feedback_fill_blank"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    source = _active_release(user, example_count=1)
    release = source.versions.first().concept_references.first().concept.release
    task = make_task_plan(learner_id=actor_id).model_copy(
        update={
            "knowledge_bundle_id": str(release.pk),
            "course_package_id": f"course-release-{release.pk}",
        }
    )
    paper = make_paper(item_id="q-1", concept_ids=["concept-1"]).model_copy(
        update={"learner_id": actor_id}
    )

    scoring = make_scoring_bundle(paper, score=0.0)
    audit = scoring.score_audit_records[0].model_copy(
        update={
            "confidence": 0.4,
            "scoring_method": "local_model",
            "review_status": "pending",
            "review_reason": ["teacher_review_required", "low_confidence"],
            "criterion_scores": [
                scoring.score_audit_records[0].criterion_scores[0].model_copy(
                    update={"reason": "学生答案只覆盖了参考答案的一部分。"}
                )
            ],
        },
        deep=True,
    )
    scoring = scoring.model_copy(update={"score_audit_records": [audit]}, deep=True)
    detail = question_feedback_details(
        task=task,
        paper=paper,
        scoring=scoring,
        answers={paper.all_items()[0].item_instance_id: "填写答案"},
    )[0]

    assert detail.requires_ai_assessment is True
    assert "学生答案只覆盖了参考答案的一部分" in detail.ai_assessment
    assert detail.ai_assessment.endswith(
        "ai评分置信度不足 建议通知相应教师进行重新评分"
    )
    assert detail.ai_confidence == pytest.approx(0.4)


def test_exact_constructed_response_reports_no_ai_comment_and_confidence_one() -> None:
    actor_id = "pseudonym_feedback_exact_match"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    source = _active_release(user, example_count=1)
    release = source.versions.first().concept_references.first().concept.release
    task = make_task_plan(learner_id=actor_id).model_copy(
        update={
            "knowledge_bundle_id": str(release.pk),
            "course_package_id": f"course-release-{release.pk}",
        }
    )
    paper = make_paper(item_id="q-1", concept_ids=["concept-1"]).model_copy(
        update={"learner_id": actor_id}
    )

    detail = question_feedback_details(
        task=task,
        paper=paper,
        scoring=make_scoring_bundle(paper, score=1.0),
    )[0]

    assert detail.requires_ai_assessment is True
    assert detail.ai_assessment == "无"
    assert detail.ai_confidence == 1.0
