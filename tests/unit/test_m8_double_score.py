"""Independent rubric scores keep the first result and escalate disagreement."""

from __future__ import annotations

from datetime import timedelta

from course_insight.contracts.assessment import (
    CriterionScore,
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.modules.m8_assessment_scoring.service import (
    M8AssessmentService,
)
from tests.factories.m5_m8 import UTC_TIME, make_paper, make_rubric


def _task_and_result(*, score: float, confidence: float = 0.9):
    paper = make_paper(subjective=True)
    instance = paper.all_items()[0]
    task = RubricScoringTask(
        scoring_task_id="scoring_attempt_1_item",
        attempt_id="attempt_1",
        paper_id=paper.paper_id,
        item_instance=instance,
        student_answer="working",
        rubric=make_rubric(),
        evidence_query_id="evidence_query_1",
        created_at=UTC_TIME,
    )
    result = RubricScoringResult(
        scoring_task_id=task.scoring_task_id,
        criterion_scores=[
            CriterionScore(
                criterion_id="criterion_1",
                score=score,
                student_evidence="working",
                course_evidence_id="evidence_1",
                reason="First independent score.",
            )
        ],
        total_score=score,
        confidence=confidence,
        missing_concept_ids=[],
        review_flags=[],
        model_name="local-test",
        model_version="1.0.0",
        scored_at=UTC_TIME + timedelta(seconds=1),
    )
    return task, result


def test_agreement_keeps_the_first_score_without_averaging() -> None:
    task, first = _task_and_result(score=1.0)
    _, second = _task_and_result(score=1.0)
    second = second.model_copy(update={"scored_at": UTC_TIME + timedelta(seconds=2)})

    merged = M8AssessmentService.merge_independent_rubric_results(
        task,
        first,
        second,
    )

    assert merged.total_score == 1.0
    assert merged.criterion_scores[0].score == 1.0
    assert "double_score_disagreement" not in merged.review_flags


def test_disagreement_keeps_first_score_and_flags_review() -> None:
    task, first = _task_and_result(score=1.0, confidence=0.95)
    _, second = _task_and_result(score=0.0, confidence=0.94)

    merged = M8AssessmentService.merge_independent_rubric_results(
        task,
        first,
        second,
    )

    assert merged.total_score == 1.0
    assert merged.criterion_scores[0].score == 1.0
    assert merged.confidence == 0.94
    assert "double_score_disagreement" in merged.review_flags
    assert "0.0" in merged.criterion_scores[0].reason
