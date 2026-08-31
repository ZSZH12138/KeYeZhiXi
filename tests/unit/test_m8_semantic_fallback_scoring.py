"""Acceptance tests for the deterministic-first scoring router."""

from __future__ import annotations

from course_insight.contracts.platform import AssessmentSubmission
from tests.factories.m5_m8 import (
    UTC_TIME,
    make_knowledge_bundle,
    make_m8_test_service,
    make_paper,
)


def _submission(paper, answer: str) -> AssessmentSubmission:
    return AssessmentSubmission(
        submission_id="submission_semantic_router",
        attempt_id="attempt_semantic_router",
        paper_id=paper.paper_id,
        learner_id=paper.learner_id,
        answers={paper.all_items()[0].item_instance_id: answer},
        submitted_at=UTC_TIME,
    )


def _constructed_response_bundle(*, item_type: str):
    knowledge = make_knowledge_bundle(subjective=True)
    item = knowledge.items[0].model_copy(
        update={
            "item_type": item_type,
            "answer_key": {"answers": ["标准答案"]},
        },
        deep=True,
    )
    return knowledge.model_copy(update={"items": [item]}, deep=True)


def test_constructed_response_exact_match_is_full_credit_without_ai() -> None:
    service = make_m8_test_service()
    paper = make_paper(subjective=True)
    knowledge = _constructed_response_bundle(item_type="short_answer")

    prepared = service.prepare_scoring(
        paper,
        _submission(paper, "标准答案"),
        knowledge,
    )

    assert prepared.rubric_scoring_tasks == []
    assert prepared.objective_audit_records[0].total_score == 1.0
    assert prepared.objective_audit_records[0].confidence == 1.0
    assert prepared.objective_audit_records[0].review_status == "not_required"


def test_constructed_response_mismatch_routes_to_deepseek_task() -> None:
    service = make_m8_test_service()
    paper = make_paper(subjective=True)
    knowledge = _constructed_response_bundle(item_type="short_answer")

    prepared = service.prepare_scoring(
        paper,
        _submission(paper, "意思接近但字符串不同"),
        knowledge,
    )

    assert prepared.objective_audit_records == []
    assert len(prepared.rubric_scoring_tasks) == 1
    task = prepared.rubric_scoring_tasks[0]
    assert task.question_type == "subjective"
    assert task.reference_answers == ["标准答案"]
    assert task.concept_names == ["Concept 2"]
    assert task.review_confidence_threshold == 0.5


def test_constructed_response_uses_frozen_rubric_review_threshold() -> None:
    service = make_m8_test_service()
    paper = make_paper(subjective=True)
    knowledge = _constructed_response_bundle(item_type="short_answer")
    rubric = knowledge.rubrics[0]
    rubric = rubric.model_copy(
        update={
            "review_policy": rubric.review_policy.model_copy(
                update={"low_confidence_threshold": 0.7},
                deep=True,
            )
        },
        deep=True,
    )
    knowledge = knowledge.model_copy(update={"rubrics": [rubric]}, deep=True)

    prepared = service.prepare_scoring(
        paper,
        _submission(paper, "意思接近但字符串不同"),
        knowledge,
    )

    assert prepared.rubric_scoring_tasks[0].review_confidence_threshold == 0.7


def test_fill_blank_mismatch_gets_a_synthetic_deepseek_rubric() -> None:
    service = make_m8_test_service()
    paper = make_paper(subjective=False)
    knowledge = _constructed_response_bundle(item_type="fill_blank")
    item = knowledge.items[0].model_copy(update={"rubric_id": None}, deep=True)
    knowledge = knowledge.model_copy(
        update={"items": [item], "rubrics": []},
        deep=True,
    )

    prepared = service.prepare_scoring(
        paper,
        _submission(paper, "不同答案"),
        knowledge,
    )

    assert prepared.objective_audit_records == []
    task = prepared.rubric_scoring_tasks[0]
    assert task.question_type == "fill_blank"
    assert task.max_score() == paper.all_items()[0].max_score
    assert task.item_instance.rubric_id == task.rubric.rubric_id


def test_choice_mismatch_remains_zero_without_ai() -> None:
    service = make_m8_test_service()
    paper = make_paper(subjective=False)
    knowledge = make_knowledge_bundle(subjective=False)

    prepared = service.prepare_scoring(
        paper,
        _submission(paper, "no"),
        knowledge,
    )

    assert prepared.rubric_scoring_tasks == []
    assert prepared.objective_audit_records[0].total_score == 0.0
    assert prepared.objective_audit_records[0].confidence == 1.0
