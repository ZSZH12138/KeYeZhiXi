"""Confirmed scores open correction; unconfirmed scores stay closed."""

from __future__ import annotations

from course_insight.contracts.tutoring import STUDENT_CITATION_QUOTE_PLACEHOLDER
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    feedback_view,
    student_result_view,
)
from tests.factories.m5_m8 import make_paper, make_scoring_bundle
from tests.integration._django_web_support import feedback_for


def test_confirmed_partial_score_opens_correction() -> None:
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper, score=0.0)
    view = student_result_view(paper, scoring, feedback_for(paper.learner_id))

    assert view.score_pending_rescore is False
    assert view.correction_available is True
    assert view.feedback is not None
    assert view.feedback.correction_note == "尚未订正"


def test_pending_review_does_not_open_correction() -> None:
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper, score=0.0)
    audit = scoring.score_audit_records[0].model_copy(
        update={
            "review_status": "pending",
            "review_reason": ["teacher_review_required"],
        }
    )
    pending = scoring.model_copy(update={"score_audit_records": [audit]})
    view = student_result_view(paper, pending, feedback_for(paper.learner_id))

    assert view.score_pending_rescore is True
    assert view.correction_available is False
    assert view.feedback is None


def test_feedback_keeps_locator_when_quote_is_placeholder() -> None:
    package = feedback_for("learner_1")
    citation = package.evidence_citations[0].model_copy(
        update={"quote": STUDENT_CITATION_QUOTE_PLACEHOLDER}
    )
    package = package.model_copy(update={"evidence_citations": [citation]})
    view = feedback_view(package, correction_note="尚未订正")

    assert view.citations[0].quote == STUDENT_CITATION_QUOTE_PLACEHOLDER
    assert "@" in view.citations[0].label
    assert view.correction_note == "尚未订正"
