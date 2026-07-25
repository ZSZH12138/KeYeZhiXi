from __future__ import annotations

import pytest
from django.http import QueryDict

from course_insight.modules.m0_platform.django_app.forms.start import (
    AssessmentStartForm,
    ReviewLookupForm,
    ScopeSelectionForm,
)


def test_start_form_accepts_only_bounded_assessment_intent() -> None:
    form = AssessmentStartForm(
        data={
            "student_text": "  请生成一次练习。  ",
            "task_type_hint": "practice",
            "flow_token": "signed-value",
        }
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data["student_text"] == "请生成一次练习。"


def test_start_form_rejects_extra_duplicate_or_non_assessment_input() -> None:
    extra = AssessmentStartForm(
        data={
            "student_text": "practice",
            "task_type_hint": "practice",
            "flow_token": "signed-value",
            "learner_id": "pseudonym_other",
        }
    )
    duplicate = QueryDict("", mutable=True)
    duplicate.setlist("student_text", ["first", "second"])
    duplicate["task_type_hint"] = "practice"
    duplicate["flow_token"] = "signed-value"

    assert not extra.is_valid()
    assert not AssessmentStartForm(data=duplicate).is_valid()
    assert not AssessmentStartForm(
        data={
            "student_text": "qa",
            "task_type_hint": "qa",
            "flow_token": "signed-value",
        }
    ).is_valid()


@pytest.mark.parametrize("duplicate_field", ["course_id", "class_id"])
def test_scope_selection_form_rejects_duplicate_scope_fields(
    duplicate_field: str,
) -> None:
    duplicate = QueryDict("", mutable=True)
    duplicate["course_id"] = "course_a"
    duplicate["class_id"] = "class_1"
    duplicate.setlist(duplicate_field, ["scope_1", "scope_2"])

    form = ScopeSelectionForm(data=duplicate)

    assert not form.is_valid()
    assert duplicate_field in form.errors


@pytest.mark.parametrize(
    "duplicate_field",
    ["course_id", "class_id", "paper_id"],
)
def test_review_lookup_form_rejects_duplicate_known_fields(
    duplicate_field: str,
) -> None:
    duplicate = QueryDict("", mutable=True)
    duplicate["course_id"] = "course_a"
    duplicate["class_id"] = "class_1"
    duplicate["paper_id"] = "paper_1"
    duplicate.setlist(duplicate_field, ["scope_1", "scope_2"])

    form = ReviewLookupForm(data=duplicate)

    assert not form.is_valid()
    assert duplicate_field in form.errors


@pytest.mark.parametrize(
    "form_type, values",
    [
        (
            ScopeSelectionForm,
            {"course_id": "course_a", "class_id": "class_1"},
        ),
        (
            ReviewLookupForm,
            {
                "course_id": "course_a",
                "class_id": "class_1",
                "paper_id": "paper_1",
            },
        ),
    ],
)
def test_scope_forms_reject_unknown_query_fields(
    form_type: type[ScopeSelectionForm],
    values: dict[str, str],
) -> None:
    query = QueryDict("", mutable=True)
    query.update(values)
    query["learner_id"] = "pseudonym_other"

    assert not form_type(data=query).is_valid()
