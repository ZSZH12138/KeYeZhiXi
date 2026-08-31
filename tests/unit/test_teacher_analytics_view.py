"""Teacher analytics expose the five dashboard entry points."""

from __future__ import annotations

from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    analytics_view,
)
from tests.unit.test_m9_deepseek import _analytics


def test_analytics_view_includes_map_misconceptions_students_and_suggestions() -> None:
    view = analytics_view(
        _analytics(),
        learner_account_by_id={"learner_private_scope": "student_demo1"},
    )

    assert view.report_id == "report_private_scope"
    assert view.concept_summaries[0].concept_id == "concept_private_scope"
    assert view.misconception_summaries[0].misconception_id == (
        "misconception_private_scope"
    )
    assert view.individual_reports[0].learner_id == "learner_private_scope"
    assert view.individual_reports[0].learner_account == "student_demo1"
    assert view.suggestions[0].suggestion_id == "suggestion_private_scope"
    assert view.suggestions[0].status == "candidate"
    assert view.review_count == 1


def test_analytics_view_never_falls_back_to_an_internal_learner_id() -> None:
    view = analytics_view(_analytics())

    assert view.individual_reports[0].learner_account == "账号已不可用"
