"""Confirmed scores open correction; unconfirmed scores stay closed."""

from __future__ import annotations

from course_insight.contracts.tutoring import STUDENT_CITATION_QUOTE_PLACEHOLDER
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    feedback_view,
    student_result_view,
)
from tests.factories.m5_m8 import (
    make_knowledge_bundle,
    make_paper,
    make_scoring_bundle,
)
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


def test_feedback_resolves_internal_references_for_student_display() -> None:
    package = feedback_for("learner_1").model_copy(
        update={
            "message": (
                "Compare the cited passage for concept_2, "
                "concept_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff "
                "with your reasoning."
            ),
            "missing_concept_ids": [
                "concept_2",
                "concept_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            ],
            "next_practice_item_ids": [
                "practice_concept_2",
                "practice_concept_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            ],
        }
    )

    view = feedback_view(package, knowledge_bundle=make_knowledge_bundle())

    assert view.message == (
        "请结合下方课程原文，重点复习：Concept 2。"
        "先确认题目条件，再检查自己的推理过程和需要修改的步骤。"
    )
    assert view.missing_concept_names == ("Concept 2",)
    assert view.next_practice_labels == ("“Concept 2”专项练习",)
    assert "concept_" not in view.message


def test_feedback_omits_unresolvable_internal_references() -> None:
    opaque_id = (
        "concept_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )
    package = feedback_for("learner_1").model_copy(
        update={
            "message": f"Review the cited passage for {opaque_id}.",
            "missing_concept_ids": [opaque_id],
            "next_practice_item_ids": [f"practice_{opaque_id}"],
        }
    )

    view = feedback_view(package, knowledge_bundle=make_knowledge_bundle())

    assert view.message is None
    assert view.missing_concept_names == ()
    assert view.next_practice_labels == ()
