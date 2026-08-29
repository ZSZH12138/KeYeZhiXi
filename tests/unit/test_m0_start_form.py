from __future__ import annotations

import pytest
from django.http import QueryDict

from course_insight.modules.m0_platform.django_app.forms.start import (
    AssessmentStartForm,
    ReviewLookupForm,
    ScopeSelectionForm,
)


def test_start_form_accepts_task_type_without_free_text() -> None:
    form = AssessmentStartForm(
        data={
            "task_type_hint": "practice",
            "flow_token": "signed-value",
        }
    )

    assert form.is_valid(), form.errors
    assert "student_text" not in form.fields


def test_start_form_rejects_extra_duplicate_or_non_assessment_input() -> None:
    extra = AssessmentStartForm(
        data={
            "task_type_hint": "practice",
            "flow_token": "signed-value",
            "learner_id": "pseudonym_other",
        }
    )
    duplicate = QueryDict("", mutable=True)
    duplicate.setlist("task_type_hint", ["practice", "diagnostic"])
    duplicate["flow_token"] = "signed-value"

    assert not extra.is_valid()
    assert not AssessmentStartForm(data=duplicate).is_valid()
    assert not AssessmentStartForm(
        data={
            "task_type_hint": "qa",
            "flow_token": "signed-value",
        }
    ).is_valid()
    assert not AssessmentStartForm(
        data={
            "student_text": "这个字段不应再由学生提交",
            "task_type_hint": "practice",
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
    ["course_id", "class_id", "learner_account"],
)
def test_review_lookup_form_rejects_duplicate_known_fields(
    duplicate_field: str,
) -> None:
    duplicate = QueryDict("", mutable=True)
    duplicate["course_id"] = "course_a"
    duplicate["class_id"] = "class_1"
    duplicate["learner_account"] = "pseudonym_student_1"
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
                "learner_account": "pseudonym_student_1",
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
    query["paper_id"] = "paper_1"

    assert not form_type(data=query).is_valid()


def test_review_lookup_form_requires_the_student_account_instead_of_a_paper_id() -> None:
    form = ReviewLookupForm(
        data={
            "course_id": "course_a",
            "class_id": "class_1",
            "learner_account": "pseudonym_student_1",
        }
    )

    assert form.is_valid(), form.errors
