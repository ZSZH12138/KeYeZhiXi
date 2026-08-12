from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from course_insight.application.coordinator import AppCoordinator
from course_insight.contracts.analytics import CriterionOverride
from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    ItemInstance,
    PaperSection,
    RemediationPlan,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.knowledge import AssessmentBlueprint, ItemCard, KnowledgeBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.platform import AssessmentSubmission, TeacherReviewSubmission
from course_insight.modules.m8_assessment_scoring.stubs import M8AssessmentServiceStub
from course_insight.modules.m9_teacher_analytics.stubs import M9TeacherAnalyticsServiceStub


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _objective_paper() -> AssessmentPaper:
    item = ItemInstance(
        item_instance_id="instance_1",
        item_id="item_1",
        item_version="1.0.0",
        stem="Choose the governed answer.",
        parameters={},
        concept_ids=["concept_1"],
        rubric_id=None,
        max_score=1.0,
        source_evidence_ids=["evidence_1"],
    )
    paper = AssessmentPaper(
        paper_id="paper_1",
        task_id="task_1",
        blueprint_id="blueprint_1",
        blueprint_version="1.0.0",
        learner_id="pseudonym_learner",
        sections=[
            PaperSection(
                section_id="section_1",
                name="Objective",
                items=[item],
                score=1.0,
            )
        ],
        generated_at=NOW,
        immutable_checksum="pending",
    )
    paper.immutable_checksum = paper.freeze()
    return paper


def _objective_knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle.model_construct(
        knowledge_bundle_id="bundle_with_independent_identity",
        course_package_id="course_package_authoritative",
        course_id="course_1",
        items=[
            ItemCard.model_construct(
                item_id="item_1",
                version="1.0.0",
                item_type="true_false",
                answer_key={"answer": False, "max_score": 1.0},
            )
        ],
        rubrics=[],
        blueprints=[
            AssessmentBlueprint.model_construct(blueprint_id="blueprint_1")
        ],
    )


def _scoring_bundle() -> ScoringResultBundle:
    audit = ScoreAuditRecord(
        audit_id="audit_1",
        audit_version=1,
        attempt_id="attempt_1",
        item_instance_id="instance_1",
        criterion_scores=[
            CriterionScore(
                criterion_id="objective_item_1",
                score=1.0,
                student_evidence="false",
                course_evidence_id="evidence_1",
                reason="Matches the governed answer.",
            )
        ],
        total_score=1.0,
        max_score=1.0,
        confidence=1.0,
        scoring_method="rule",
        review_status="pending",
        review_reason=["teacher_confirmation"],
        created_at=NOW,
    )
    return ScoringResultBundle(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="pseudonym_learner",
        score_audit_records=[audit],
        learning_events=[],
        remediation_plan=RemediationPlan(
            plan_id="remediation_1",
            based_on_attempt_id="attempt_1",
            learner_id="pseudonym_learner",
            targets=[],
            created_at=NOW,
        ),
        total_score=1.0,
        max_score=1.0,
        finalized_at=NOW,
    )


def test_assessment_submission_preserves_supported_scalar_answers() -> None:
    submission = AssessmentSubmission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="pseudonym_learner",
        answers={
            "instance_bool": False,
            "instance_int": 2,
            "instance_float": 2.5,
            "instance_text": "visible answer",
        },
        submitted_at=NOW,
    )

    assert submission.answers == {
        "instance_bool": False,
        "instance_int": 2,
        "instance_float": 2.5,
        "instance_text": "visible answer",
    }


@pytest.mark.parametrize(
    ("item_id", "answer"),
    [("", "answer"), ("instance_1", float("inf")), ("instance_1", float("nan"))],
)
def test_assessment_submission_rejects_invalid_scalar_boundaries(
    item_id: str,
    answer: str | float,
) -> None:
    with pytest.raises(ValidationError):
        AssessmentSubmission(
            submission_id="submission_invalid",
            attempt_id="attempt_1",
            paper_id="paper_1",
            learner_id="pseudonym_learner",
            answers={item_id: answer},
            submitted_at=NOW,
        )


def test_assessment_submission_schema_rejects_blank_answer_keys() -> None:
    answer_schema = AssessmentSubmission.model_json_schema()["properties"][
        "answers"
    ]

    assert answer_schema["propertyNames"] == {"pattern": r"\S"}


def test_m8_prepare_scoring_consumes_path_free_assessment_submission() -> None:
    submission = AssessmentSubmission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="pseudonym_learner",
        answers={"instance_1": False},
        submitted_at=NOW,
    )

    result = M8AssessmentServiceStub().prepare_scoring(
        assessment_paper=_objective_paper(),
        raw_answer_path=submission,
        knowledge_bundle=_objective_knowledge_bundle(),
    )

    assert result.attempt_id == "attempt_1"
    assert result.objective_audit_records[0].total_score == 1.0


def test_m8_prepare_scoring_preserves_json_path_boundary(tmp_path: Path) -> None:
    answer_path = tmp_path / "student_answers.json"
    answer_path.write_text(
        json.dumps(
            {
                "attempt_id": "attempt_1",
                "paper_id": "paper_1",
                "learner_id": "pseudonym_learner",
                "answers": [
                    {"item_instance_id": "instance_1", "answer": False}
                ],
            }
        ),
        encoding="utf-8",
    )

    result = M8AssessmentServiceStub().prepare_scoring(
        assessment_paper=_objective_paper(),
        raw_answer_path=answer_path,
        knowledge_bundle=_objective_knowledge_bundle(),
    )

    assert result.attempt_id == "attempt_1"
    assert result.objective_audit_records[0].total_score == 1.0


def test_teacher_review_submission_uses_domain_decision_vocabulary() -> None:
    submission = TeacherReviewSubmission(
        submission_id="decision_1",
        audit_id="audit_1",
        expected_audit_version=1,
        expected_audit_checksum="0" * 64,
        reviewer_id="pseudonym_teacher",
        decision="confirm",
        final_total_score=1.0,
        criterion_overrides=[],
        teacher_comment="Confirmed against the governed evidence.",
        submitted_at=NOW,
    )

    assert submission.decision == "confirm"


def test_m9_record_teacher_review_consumes_path_free_submission() -> None:
    submission = TeacherReviewSubmission(
        submission_id="decision_1",
        audit_id="audit_1",
        expected_audit_version=1,
        expected_audit_checksum=(
            _scoring_bundle().get_audit_record("audit_1").content_checksum()
        ),
        reviewer_id="pseudonym_teacher",
        decision="confirm",
        final_total_score=1.0,
        criterion_overrides=[],
        teacher_comment="Confirmed against the governed evidence.",
        submitted_at=NOW,
    )

    decision = M9TeacherAnalyticsServiceStub().record_teacher_review(
        raw_review_path=submission,
        current_scoring_result_bundle=_scoring_bundle(),
    )

    assert decision.decision_id == submission.submission_id
    assert decision.decision == "confirm"
    assert decision.reviewed_at == submission.submitted_at


def test_m9_record_teacher_review_preserves_json_path_boundary(
    tmp_path: Path,
) -> None:
    review_path = tmp_path / "teacher_review.json"
    review_path.write_text(
        json.dumps(
            {
                "decision_id": "decision_path_1",
                "audit_id": "audit_1",
                "expected_audit_version": 1,
                "expected_audit_checksum": (
                    _scoring_bundle()
                    .get_audit_record("audit_1")
                    .content_checksum()
                ),
                "decision": "confirm",
                "final_total_score": 1.0,
                "criterion_overrides": [],
                "teacher_comment": "Confirmed from the governed JSON form.",
                "reviewer_id": "pseudonym_teacher",
                "reviewed_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    decision = M9TeacherAnalyticsServiceStub().record_teacher_review(
        raw_review_path=review_path,
        current_scoring_result_bundle=_scoring_bundle(),
    )

    assert decision.decision_id == "decision_path_1"
    assert decision.decision == "confirm"


def test_teacher_override_submission_reuses_existing_override_contract() -> None:
    override = CriterionOverride(
        criterion_id="criterion_1",
        previous_score=0.0,
        new_score=1.0,
        reason="Teacher correction.",
    )

    submission = TeacherReviewSubmission(
        submission_id="decision_2",
        audit_id="audit_2",
        expected_audit_version=1,
        expected_audit_checksum="0" * 64,
        reviewer_id="pseudonym_teacher",
        decision="override",
        final_total_score=1.0,
        criterion_overrides=[override],
        teacher_comment="Corrected against the rubric.",
        submitted_at=NOW,
    )

    assert submission.criterion_overrides == [override]


def test_coordinator_declares_both_path_free_submission_boundaries() -> None:
    assessment_hints = get_type_hints(AppCoordinator.run_assessment_cycle)
    review_hints = get_type_hints(AppCoordinator.run_teacher_review_cycle)

    assert assessment_hints["raw_answer_path"] == Path | AssessmentSubmission
    assert review_hints["raw_review_path"] == Path | TeacherReviewSubmission


def test_invalid_review_path_does_not_leak_host_location(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-review.json"

    with pytest.raises(DomainError) as captured:
        M9TeacherAnalyticsServiceStub().record_teacher_review(
            raw_review_path=missing_path,
            current_scoring_result_bundle=_scoring_bundle(),
        )

    assert captured.value.code == "REPORT_SCOPE_INVALID"
    assert captured.value.details == {"file_name": "missing-review.json"}
