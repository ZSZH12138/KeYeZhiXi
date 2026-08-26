from __future__ import annotations

import pytest

from course_insight.modules.m0_platform.django_app.assessment_feedback import (
    question_feedback_details,
)
from course_insight.modules.m0_platform.django_app.models import User
from tests.factories.m5_m8 import make_paper, make_task_plan
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

    details = question_feedback_details(task=task, paper=paper)

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
    assert details[0].source_blocks[0].file_name == "chapter.txt"
    assert details[0].source_blocks[0].text == "拥塞控制用于避免网络过载。"
    assert "B. 无连接并保留应用报文边界" in details[0].qa_prompt
    assert "正确答案：B" in details[0].qa_prompt

    source.status = "deleted"
    source.save(update_fields=("status", "updated_at"))
    assert question_feedback_details(task=task, paper=paper)[0].source_blocks == ()
