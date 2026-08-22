from __future__ import annotations

from datetime import timedelta

import pytest

from course_insight.contracts.analytics import TeacherReviewDecision
from course_insight.contracts.assessment import (
    CriterionScore,
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.contracts.errors import DomainError
from tests.factories.m5_m8 import (
    UTC_TIME,
    make_m8_test_service,
    make_paper,
    make_rubric,
    make_scoring_bundle,
)


def _pending_bundle(paper):
    bundle = make_scoring_bundle(paper, score=1.0)
    audit = bundle.score_audit_records[0]
    pending = audit.model_copy(
        update={
            "scoring_method": "local_model",
            "review_status": "pending",
            "review_reason": ["teacher_review_required"],
            "criterion_scores": [
                CriterionScore(
                    criterion_id="criterion_1",
                    score=1.0,
                    student_evidence="working",
                    course_evidence_id="evidence_1",
                    reason="Model awarded the criterion.",
                )
            ],
        }
    )
    return bundle.model_copy(update={"score_audit_records": [pending]})


def _task_and_result(paper, attempt_id: str):
    instance = paper.all_items()[0]
    task = RubricScoringTask(
        scoring_task_id=f"scoring_{attempt_id}_{instance.item_instance_id}",
        attempt_id=attempt_id,
        paper_id=paper.paper_id,
        item_instance=instance,
        student_answer="working",
        rubric=make_rubric(),
        evidence_query_id=(
            f"evidence_query_{attempt_id}_{instance.item_instance_id}"
        ),
        created_at=UTC_TIME,
    )
    result = RubricScoringResult(
        scoring_task_id=task.scoring_task_id,
        criterion_scores=[
            CriterionScore(
                criterion_id="criterion_1",
                score=1.0,
                student_evidence="working",
                course_evidence_id="evidence_1",
                reason="Rescore awarded the criterion.",
            )
        ],
        total_score=1.0,
        confidence=0.9,
        missing_concept_ids=[],
        review_flags=[],
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        scored_at=UTC_TIME + timedelta(minutes=2),
    )
    return task, result


def test_model_rescore_appends_pending_version_and_keeps_rejected_history() -> None:
    service = make_m8_test_service()
    paper = make_paper(subjective=True)
    bundle = _pending_bundle(paper)
    pending = bundle.score_audit_records[0]
    rejected = service.apply_teacher_review(
        bundle,
        TeacherReviewDecision(
            decision_id="review_reject_rescore",
            audit_id=pending.audit_id,
            expected_audit_version=pending.audit_version,
            expected_audit_checksum=pending.content_checksum(),
            decision="reject",
            final_total_score=pending.total_score,
            criterion_overrides=[],
            teacher_comment="Reject for rescore.",
            reviewer_id="teacher_1",
            reviewed_at=UTC_TIME + timedelta(minutes=1),
        ),
    )
    rejected_audit = rejected.get_audit_record(pending.audit_id)
    task, result = _task_and_result(paper, rejected.attempt_id)

    with pytest.raises(DomainError) as mismatch:
        service.apply_model_rescore(
            rejected,
            task,
            result,
            audit_id=rejected_audit.audit_id,
            expected_rejected_version=rejected_audit.audit_version,
            expected_rejected_checksum=rejected_audit.content_checksum(),
            expected_raw_answer_checksum="c" * 64,
            raw_answer_checksum="d" * 64,
            rescore_request_id="rescore_unit_bad",
        )
    assert mismatch.value.code == "RESCORE_INPUT_MISMATCH"

    rescored = service.apply_model_rescore(
        rejected,
        task,
        result,
        audit_id=rejected_audit.audit_id,
        expected_rejected_version=rejected_audit.audit_version,
        expected_rejected_checksum=rejected_audit.content_checksum(),
        expected_raw_answer_checksum="c" * 64,
        raw_answer_checksum="c" * 64,
        rescore_request_id="rescore_unit_1",
    )
    latest = rescored.get_audit_record(rejected_audit.audit_id)
    history = [
        record
        for record in rescored.score_audit_records
        if record.audit_id == rejected_audit.audit_id
    ]

    assert rejected_audit.is_rejected()
    assert latest.scoring_method == "local_model_rescore"
    assert latest.review_status == "pending"
    assert latest.audit_version == rejected_audit.audit_version + 1
    assert [record.audit_version for record in history] == [1, 2, 3]
    assert history[1].scoring_method == "teacher_override"
    assert history[1].is_rejected()
