from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    InMemoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _workflow() -> TeacherReviewWorkflow:
    return TeacherReviewWorkflow(InMemoryTeacherReviewRepository())


def test_review_requires_explicit_submission_before_approval() -> None:
    workflow = _workflow()
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="a" * 64,
        validation_report_ref="report-1",
        now=NOW,
    )

    with pytest.raises(DomainError, match="invalid state transition"):
        workflow.approve(
            "review-1",
            reviewer_pseudonym="teacher-1",
            reason="approved",
            expected_version=draft.version,
            now=NOW,
        )

    submitted = workflow.submit(
        "review-1",
        reviewer_pseudonym="teacher-1",
        reason="checked source bindings",
        expected_version=draft.version,
        now=NOW,
    )
    approved = workflow.approve(
        "review-1",
        reviewer_pseudonym="teacher-1",
        reason="approved",
        expected_version=submitted.version,
        now=NOW,
    )
    assert approved.state == "approved"
    assert len(approved.history) == 3
    assert approved.input_checksum == "a" * 64


def test_stale_cas_transition_and_invalid_recall_fail_closed() -> None:
    workflow = _workflow()
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="b" * 64,
        validation_report_ref="report-1",
        now=NOW,
    )
    submitted = workflow.submit(
        "review-1", "teacher-1", "checked", draft.version, NOW
    )
    with pytest.raises(DomainError, match="version conflict"):
        workflow.reject(
            "review-1", "teacher-1", "stale", draft.version, NOW
        )
    approved = workflow.approve(
        "review-1", "teacher-1", "approved", submitted.version, NOW
    )
    recalled = workflow.recall(
        "review-1", "teacher-1", "source changed", approved.version, NOW
    )
    assert recalled.state == "recalled"
    with pytest.raises(DomainError, match="invalid state transition"):
        workflow.recall(
            "review-1", "teacher-1", "again", recalled.version, NOW
        )


def test_duplicate_transition_is_idempotent_and_history_is_immutable() -> None:
    repository = InMemoryTeacherReviewRepository()
    workflow = TeacherReviewWorkflow(repository)
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="c" * 64,
        validation_report_ref="report-1",
        now=NOW,
    )
    first = workflow.submit(
        "review-1", "teacher-1", "checked", draft.version, NOW
    )
    replay = workflow.submit(
        "review-1", "teacher-1", "checked", draft.version, NOW
    )
    assert replay == first
    assert repository.get("review-1").history == first.history
    with pytest.raises(DomainError, match="not approved"):
        workflow.require_approved("review-1")


def test_review_rejects_invalid_checksum_pseudonym_and_unbounded_reason() -> None:
    workflow = _workflow()
    with pytest.raises(DomainError, match="review payload is invalid"):
        workflow.create_draft(
            review_id="review-1",
            subject_id="course-1",
            input_checksum="not-a-checksum",
            validation_report_ref="report-1",
            now=NOW,
        )
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="d" * 64,
        validation_report_ref="report-1",
        now=NOW,
    )
    with pytest.raises(DomainError, match="review payload is invalid"):
        workflow.submit("review-1", "", "checked", draft.version, NOW)
    with pytest.raises(DomainError, match="review payload is invalid"):
        workflow.submit("review-1", "teacher-1", "x" * 2001, draft.version, NOW)


def test_approved_gate_calls_publisher_once_and_recalled_review_cannot_publish() -> None:
    workflow = _workflow()
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="e" * 64,
        validation_report_ref="report-1",
        now=NOW,
    )
    submitted = workflow.submit(
        "review-1", "teacher-1", "checked", draft.version, NOW
    )
    approved = workflow.approve(
        "review-1", "teacher-1", "approved", submitted.version, NOW
    )
    published: list[str] = []
    assert workflow.publish_approved(
        "review-1", lambda: published.append("bundle"), expected_version=approved.version
    ) is None
    assert published == ["bundle"]
    recalled = workflow.recall(
        "review-1", "teacher-1", "source changed", approved.version, NOW
    )
    assert recalled.state == "recalled"
    with pytest.raises(DomainError, match="not approved"):
        workflow.publish_approved("review-1", lambda: published.append("bad"))
    assert published == ["bundle"]
