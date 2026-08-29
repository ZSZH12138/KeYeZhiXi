"""AI scoring failure remains auditable and always enters teacher review."""

from __future__ import annotations

from datetime import timedelta

from course_insight.contracts.assessment import RubricScoringTask
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from tests.factories.m5_m8 import FixedClock, UTC_TIME, make_paper, make_rubric


def _task() -> RubricScoringTask:
    paper = make_paper(subjective=True)
    return RubricScoringTask(
        scoring_task_id="scoring_attempt_1_item",
        attempt_id="attempt_1",
        paper_id=paper.paper_id,
        item_instance=paper.all_items()[0],
        student_answer="working",
        reference_answers=["reference"],
        concept_names=["Concept 2"],
        rubric=make_rubric(),
        evidence_query_id="evidence_query_1",
        created_at=UTC_TIME,
    )


def test_nine_invalid_outputs_fall_back_to_zero_confidence_review() -> None:
    task = _task()
    service = M8AssessmentService(
        object(),
        object(),
        object(),
        clock=FixedClock(UTC_TIME + timedelta(seconds=3)),
    )

    deferred = service.defer_rubric_scoring(task, reason_code="INVALID_MODEL_JSON")

    assert deferred.total_score == 0.0
    assert deferred.confidence == 0.0
    assert deferred.review_flags == [
        "teacher_review_required",
        "low_confidence",
        "ai_output_invalid",
    ]
    assert deferred.criterion_scores[0].student_evidence == task.student_answer
    assert deferred.criterion_scores[0].reason.startswith("AI评分生成错误")
    assert deferred.criterion_scores[0].reason.endswith(
        "ai评分置信度不足 建议通知相应教师进行重新评分"
    )
