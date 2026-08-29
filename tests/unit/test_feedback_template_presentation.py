"""Presentation contracts for AI comments and learner wrong-item questions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "course_insight"
    / "modules"
    / "m0_platform"
    / "django_app"
    / "templates"
    / "course_insight"
)


@pytest.mark.parametrize(
    ("relative_path", "binding"),
    (
        ("teacher/context.html", "{{ question.detail.ai_assessment }}"),
        (
            "teacher/suggested_review_detail.html",
            "{{ question.detail.ai_assessment }}",
        ),
        ("student/result.html", "{{ item.ai_assessment }}"),
        ("student/pending_result.html", "{{ item.ai_assessment }}"),
        ("student/feedback.html", "{{ item.ai_assessment }}"),
    ),
)
def test_ai_assessment_is_plain_text_in_feedback_templates(
    relative_path: str,
    binding: str,
) -> None:
    template = (TEMPLATE_ROOT / relative_path).read_text(encoding="utf-8")

    assert f"<p>{binding}</p>" in template
    assert re.search(
        rf"<textarea[^>]*>\s*{re.escape(binding)}",
        template,
    ) is None


def test_teacher_learner_page_labels_every_wrong_item_question() -> None:
    template = (TEMPLATE_ROOT / "teacher/learner.html").read_text(
        encoding="utf-8"
    )

    assert (
        '<strong>题目：</strong>{{ item.stem|default:"题目内容暂不可用" }}'
        in template
    )
