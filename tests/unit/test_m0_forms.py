from __future__ import annotations

from datetime import datetime, timezone

import pytest
from django.http import QueryDict

from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    ItemInstance,
    PaperSection,
    ScoreAuditRecord,
)
from course_insight.contracts.platform import (
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.modules.m0_platform.django_app.forms import (
    AssessmentSubmissionForm,
    TeacherReviewForm,
)


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _paper() -> AssessmentPaper:
    items = [
        ItemInstance(
            item_instance_id="objective_bool",
            item_id="item_bool",
            item_version="1.0.0",
            stem="Is the statement correct?",
            parameters={"answer_type": "bool"},
            concept_ids=["concept_1"],
            rubric_id=None,
            max_score=1.0,
            source_evidence_ids=["evidence_1"],
        ),
        ItemInstance(
            item_instance_id="subjective_text",
            item_id="item_text",
            item_version="1.0.0",
            stem="Explain your reasoning.",
            parameters={},
            concept_ids=["concept_1"],
            rubric_id="rubric_1",
            max_score=4.0,
            source_evidence_ids=["evidence_1"],
        ),
    ]
    paper = AssessmentPaper(
        paper_id="paper_1",
        task_id="task_1",
        blueprint_id="blueprint_1",
        blueprint_version="1.0.0",
        learner_id="pseudonym_student_001",
        sections=[
            PaperSection(
                section_id="section_1",
                name="Assessment",
                items=items,
                score=5.0,
            )
        ],
        generated_at=NOW,
        immutable_checksum="pending",
    )
    return paper.model_copy(
        update={"immutable_checksum": paper.freeze()},
        deep=True,
    )


def _audit() -> ScoreAuditRecord:
    return ScoreAuditRecord(
        audit_id="audit_1",
        audit_version=3,
        attempt_id="attempt_1",
        item_instance_id="subjective_text",
        criterion_scores=[
            CriterionScore(
                criterion_id="accuracy",
                score=1.5,
                student_evidence="safe excerpt",
                course_evidence_id="evidence_1",
                reason="aligned",
            ),
            CriterionScore(
                criterion_id="reasoning",
                score=1.0,
                student_evidence="safe excerpt",
                course_evidence_id="evidence_1",
                reason="partial",
            ),
        ],
        total_score=2.5,
        max_score=4.0,
        confidence=0.6,
        scoring_method="local_model",
        review_status="pending",
        review_reason=["teacher_review"],
        created_at=NOW,
    )


def _numeric_paper() -> AssessmentPaper:
    items = [
        ItemInstance(
            item_instance_id="objective_int",
            item_id="item_int",
            item_version="1.0.0",
            stem="Enter an integer.",
            parameters={"answer_type": "int"},
            concept_ids=["concept_1"],
            rubric_id=None,
            max_score=1.0,
            source_evidence_ids=[],
        ),
        ItemInstance(
            item_instance_id="objective_float",
            item_id="item_float",
            item_version="1.0.0",
            stem="Enter a finite number.",
            parameters={"answer_type": "float"},
            concept_ids=["concept_1"],
            rubric_id=None,
            max_score=1.0,
            source_evidence_ids=[],
        ),
    ]
    paper = AssessmentPaper(
        paper_id="paper_numeric",
        task_id="task_numeric",
        blueprint_id="blueprint_1",
        blueprint_version="1.0.0",
        learner_id="pseudonym_student_001",
        sections=[
            PaperSection(
                section_id="numeric",
                name="Numeric",
                items=items,
                score=2.0,
            )
        ],
        generated_at=NOW,
        immutable_checksum="pending",
    )
    return paper.model_copy(
        update={"immutable_checksum": paper.freeze()},
        deep=True,
    )


def test_assessment_form_builds_existing_contract_from_server_owned_scope() -> None:
    paper = _paper()
    bool_field = AssessmentSubmissionForm.answer_field_name(
        "objective_bool"
    )
    text_field = AssessmentSubmissionForm.answer_field_name(
        "subjective_text"
    )
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=paper.learner_id,
        data={
            bool_field: "true",
            text_field: "  Because the evidence supports it.  ",
        },
    )

    assert form.is_valid(), form.errors
    submission = form.to_submission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        submitted_at=NOW,
    )

    assert type(submission) is AssessmentSubmission
    assert submission.paper_id == paper.paper_id
    assert submission.learner_id == paper.learner_id
    assert submission.answers == {
        "objective_bool": True,
        "subjective_text": "Because the evidence supports it.",
    }


def test_assessment_form_preserves_integer_and_finite_float_types() -> None:
    paper = _numeric_paper()
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=paper.learner_id,
        data={
            AssessmentSubmissionForm.answer_field_name(
                "objective_int"
            ): "7",
            AssessmentSubmissionForm.answer_field_name(
                "objective_float"
            ): "1.25",
        },
    )

    assert form.is_valid(), form.errors
    submission = form.to_submission(
        submission_id="submission_numeric",
        attempt_id="attempt_numeric",
        submitted_at=NOW,
    )
    assert submission.answers == {
        "objective_int": 7,
        "objective_float": 1.25,
    }
    assert type(submission.answers["objective_int"]) is int
    assert type(submission.answers["objective_float"]) is float


@pytest.mark.parametrize("unsafe", ["nan", "inf", "-inf", "1e9999"])
def test_assessment_form_rejects_non_finite_float_answers(
    unsafe: str,
) -> None:
    paper = _numeric_paper()
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=paper.learner_id,
        data={
            AssessmentSubmissionForm.answer_field_name(
                "objective_int"
            ): "7",
            AssessmentSubmissionForm.answer_field_name(
                "objective_float"
            ): unsafe,
        },
    )

    assert not form.is_valid()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data, _: {**data, "paper_id": "other_paper"},
        lambda data, _: {**data, "answer_unknown": "tampered"},
        lambda data, text: {**data, text: " "},
    ],
)
def test_assessment_form_rejects_extra_or_incomplete_answer_input(
    mutator,
) -> None:
    paper = _paper()
    bool_field = AssessmentSubmissionForm.answer_field_name(
        "objective_bool"
    )
    text_field = AssessmentSubmissionForm.answer_field_name(
        "subjective_text"
    )
    valid = {bool_field: "false", text_field: "Visible answer"}
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=paper.learner_id,
        data=mutator(valid, text_field),
    )

    assert not form.is_valid()


def test_assessment_form_rejects_scope_mismatch_before_binding_answers() -> None:
    with pytest.raises(ValueError, match="learner"):
        AssessmentSubmissionForm(
            paper=_paper(),
            learner_id="pseudonym_other_student",
            data={},
        )


def test_assessment_form_rejects_duplicate_answer_fields() -> None:
    paper = _paper()
    bool_field = AssessmentSubmissionForm.answer_field_name(
        "objective_bool"
    )
    text_field = AssessmentSubmissionForm.answer_field_name(
        "subjective_text"
    )
    data = QueryDict(mutable=True)
    data.setlist(bool_field, ["true", "false"])
    data[text_field] = "Visible answer"

    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=paper.learner_id,
        data=data,
    )

    assert not form.is_valid()


def test_teacher_override_form_builds_complete_existing_contract() -> None:
    audit = _audit()
    accuracy_score = TeacherReviewForm.score_field_name("accuracy")
    accuracy_reason = TeacherReviewForm.reason_field_name("accuracy")
    reasoning_score = TeacherReviewForm.score_field_name("reasoning")
    reasoning_reason = TeacherReviewForm.reason_field_name("reasoning")
    form = TeacherReviewForm(
        audit=audit,
        reviewer_id="pseudonym_teacher_001",
        criterion_caps={"accuracy": 2.0, "reasoning": 2.0},
        data={
            "decision": "override",
            "final_total_score": "3.5",
            "teacher_comment": "Reviewed against the approved rubric.",
            accuracy_score: "2.0",
            accuracy_reason: "Evidence supports full credit.",
            reasoning_score: "1.5",
            reasoning_reason: "Reasoning is substantially complete.",
        },
    )

    assert form.is_valid(), form.errors
    submission = form.to_submission(
        submission_id="review_1",
        submitted_at=NOW,
    )

    assert type(submission) is TeacherReviewSubmission
    assert submission.audit_id == audit.audit_id
    assert submission.expected_audit_version == audit.audit_version
    assert submission.expected_audit_checksum == audit.content_checksum()
    assert submission.final_total_score == 3.5
    assert [
        (
            override.criterion_id,
            override.previous_score,
            override.new_score,
        )
        for override in submission.criterion_overrides
    ] == [
        ("accuracy", 1.5, 2.0),
        ("reasoning", 1.0, 1.5),
    ]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data: {
            **data,
            TeacherReviewForm.score_field_name("accuracy"): "2.1",
        },
        lambda data: {
            **data,
            TeacherReviewForm.score_field_name("reasoning"): "",
        },
        lambda data: {
            **data,
            TeacherReviewForm.reason_field_name("accuracy"): "",
        },
        lambda data: {**data, "final_total_score": "3.0"},
        lambda data: {**data, "expected_audit_version": "2"},
        lambda data: {**data, "unknown": "tampered"},
    ],
)
def test_teacher_override_form_rejects_caps_completeness_sum_and_tampering(
    mutator,
) -> None:
    audit = _audit()
    data = {
        "decision": "override",
        "final_total_score": "3.5",
        "teacher_comment": "Reviewed against the approved rubric.",
        TeacherReviewForm.score_field_name("accuracy"): "2.0",
        TeacherReviewForm.reason_field_name("accuracy"): "Reason one.",
        TeacherReviewForm.score_field_name("reasoning"): "1.5",
        TeacherReviewForm.reason_field_name("reasoning"): "Reason two.",
    }
    form = TeacherReviewForm(
        audit=audit,
        reviewer_id="pseudonym_teacher_001",
        criterion_caps={"accuracy": 2.0, "reasoning": 2.0},
        data=mutator(data),
    )

    assert not form.is_valid()


def test_teacher_confirm_keeps_current_total_and_has_no_overrides() -> None:
    audit = _audit()
    form = TeacherReviewForm(
        audit=audit,
        reviewer_id="pseudonym_teacher_001",
        criterion_caps={"accuracy": 2.0, "reasoning": 2.0},
        data={
            "decision": "confirm",
            "final_total_score": str(audit.total_score),
            "teacher_comment": "Confirmed after review.",
        },
    )

    assert form.is_valid(), form.errors
    submission = form.to_submission(
        submission_id="review_confirm",
        submitted_at=NOW,
    )
    assert submission.decision == "confirm"
    assert submission.criterion_overrides == []


def test_teacher_review_form_rejects_duplicate_post_fields() -> None:
    audit = _audit()
    data = QueryDict(mutable=True)
    data["decision"] = "confirm"
    data.setlist(
        "final_total_score",
        [str(audit.total_score), str(audit.total_score)],
    )
    data["teacher_comment"] = "Confirmed after review."
    form = TeacherReviewForm(
        audit=audit,
        reviewer_id="pseudonym_teacher_001",
        criterion_caps={"accuracy": 2.0, "reasoning": 2.0},
        data=data,
    )

    assert not form.is_valid()
