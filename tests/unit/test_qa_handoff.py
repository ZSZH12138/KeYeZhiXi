from __future__ import annotations

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.django_app.qa_handoff import (
    issue_qa_handoff,
    verify_qa_handoff,
)


def test_qa_handoff_is_bound_to_scope_and_learner_and_rejects_tampering() -> None:
    token = issue_qa_handoff(
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_student_1",
        paper_id="paper_1",
        item_instance_id="item_instance_1",
    )
    assert verify_qa_handoff(
        token,
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_student_1",
        max_age_seconds=60,
    ) == ("paper_1", "item_instance_1")

    for changed in (
        token + "x",
        issue_qa_handoff(
            course_id="course_1",
            class_id="class_1",
            learner_id="pseudonym_other",
            paper_id="paper_1",
            item_instance_id="item_instance_1",
        ),
    ):
        with pytest.raises(DomainError) as captured:
            verify_qa_handoff(
                changed,
                course_id="course_1",
                class_id="class_1",
                learner_id="pseudonym_student_1",
                max_age_seconds=60,
            )
        assert captured.value.code == "QA_HANDOFF_INVALID"
