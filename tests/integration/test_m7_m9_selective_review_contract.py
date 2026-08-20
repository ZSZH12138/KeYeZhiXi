from __future__ import annotations

from datetime import datetime, timezone

from course_insight.contracts.assessment import (
    CriterionScore,
    ItemInstance,
    RubricScoringResult,
    RubricScoringTask,
    ScoringPreparationResult,
)
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.knowledge import ReviewPolicy, Rubric, RubricCriterion
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from course_insight.modules.m9_teacher_analytics.review_sampling import (
    ReviewSamplingPolicy,
    build_review_queue,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


class _M8Repository:
    def get_paper_execution_context(self, paper_id: str):
        assert paper_id == "paper_1"
        return ("course_1", "class_1")

    def insert_or_get_scoring_result(self, bundle):
        return bundle


def _task() -> RubricScoringTask:
    return RubricScoringTask(
        scoring_task_id="scoring_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        item_instance=ItemInstance(
            item_instance_id="item_1",
            item_id="item_1",
            item_version="1",
            stem="Explain.",
            parameters={},
            concept_ids=["concept_1"],
            rubric_id="rubric_1",
            max_score=1.0,
            source_evidence_ids=["evidence_1"],
        ),
        student_answer="uses evidence",
        rubric=Rubric(
            rubric_id="rubric_1",
            version="1",
            total_score=1.0,
            criteria=[
                RubricCriterion(
                    criterion_id="criterion_1",
                    description="evidence",
                    max_score=1.0,
                    expected_student_evidence="uses evidence",
                    course_evidence_ids=["evidence_1"],
                )
            ],
            review_policy=ReviewPolicy(
                low_confidence_threshold=0.7,
                double_score_disagreement_threshold=1.0,
                require_evidence_for_positive_score=True,
            ),
            status="published",
        ),
        evidence_query_id="query_1",
        created_at=NOW,
    )


def _preparation() -> ScoringPreparationResult:
    return ScoringPreparationResult(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        objective_audit_records=[],
        rubric_scoring_tasks=[_task()],
        evidence_queries=[
            EvidenceQuery(
                query_id="query_1",
                course_package_id="package_1",
                query_text="evidence",
                concept_ids=["concept_1"],
                item_id="item_1",
                use_case="grading",
                top_k=1,
                min_relevance=0.0,
            )
        ],
        raw_answer_checksum="a" * 64,
        prepared_at=NOW,
    )


def _result(*, flags: list[str], confidence: float = 0.9) -> RubricScoringResult:
    return RubricScoringResult(
        scoring_task_id="scoring_1",
        criterion_scores=[
            CriterionScore(
                criterion_id="criterion_1",
                score=1.0,
                student_evidence="uses evidence",
                course_evidence_id="evidence_1",
                reason="supported",
            )
        ],
        total_score=1.0,
        confidence=confidence,
        missing_concept_ids=[],
        review_flags=flags,
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        scored_at=NOW,
    )


def test_public_m7_flags_drive_m8_status_and_m9_queue_without_contract_changes() -> None:
    service = M8AssessmentService(_M8Repository(), object(), object())
    accepted = service.finalize_scoring(_preparation(), [_result(flags=[])])
    assert accepted.score_audit_records[0].review_status == "not_required"
    assert build_review_queue(
        accepted,
        policy=ReviewSamplingPolicy(),
    ) == []

    sampled = build_review_queue(
        accepted,
        policy=ReviewSamplingPolicy(
            hmac_key_id="sample-key-v1",
            default_not_required_rate=1.0,
        ),
        hmac_key=b"k" * 32,
    )
    assert sampled[0].review_reasons == ["quality_audit_sample"]

    pending = service.finalize_scoring(
        _preparation(),
        [_result(flags=["teacher_review_required"])],
    )
    assert pending.score_audit_records[0].review_status == "pending"
    mandatory = build_review_queue(
        pending,
        policy=ReviewSamplingPolicy(),
    )
    assert mandatory[0].review_reasons == ["teacher_review_required"]

    low_confidence = service.finalize_scoring(
        _preparation(),
        [_result(flags=[], confidence=0.6)],
    )
    assert low_confidence.score_audit_records[0].review_reason == ["low_confidence"]
