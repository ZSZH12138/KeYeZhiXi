from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.application.legacy_score_inventory import (
    inspect_legacy_score_posting,
    rebuild_legacy_score_posting,
)
from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.workflow import AssessmentRun
from tests.factories.m5_m8 import make_paper, make_scoring_bundle


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _submit_run(**updates: object) -> AssessmentRun:
    values: dict[str, object] = {
        "operation_id": "submit:legacy-1",
        "operation": "submit",
        "request_checksum": "a" * 64,
        "course_id": "course_1",
        "class_id": "class_1",
        "learner_id": "learner_1",
        "session_id": "session_1",
        "task_id": "task_1",
        "paper_id": "paper_1",
        "attempt_id": "attempt_1",
        "feedback_id": "feedback_1",
        "report_id": "report_1",
        "checkpoint": "completed",
        "status": "completed",
        "version": 4,
        "locked_by": None,
        "lease_until": None,
        "error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
        "scoring_result_checksum": "b" * 64,
        "state_version": 2,
        "knowledge_bundle_id": "bundle_1",
        "knowledge_bundle_version": "1.0.0",
        "knowledge_bundle_checksum": "c" * 64,
        "course_package_id": "package_1",
        "evidence_index_id": "index_1",
        "evidence_index_version": "1.0.0",
        "evidence_index_checksum": "d" * 64,
        "state_policy_checksum": "e" * 64,
        "teacher_policy_checksum": "f" * 64,
        "previous_state_frozen": False,
        "previous_learner_snapshot_id": None,
        "previous_learner_state_version": None,
        "previous_class_snapshot_id": None,
        "previous_class_state_version": None,
    }
    values.update(updates)
    return AssessmentRun(**values)  # type: ignore[arg-type]


def _pending_scoring():
    paper = make_paper()
    bundle = make_scoring_bundle(paper, score=1.0)
    audit = bundle.score_audit_records[0].model_copy(
        update={
            "review_status": "pending",
            "review_reason": ["teacher_review_required"],
        },
        deep=True,
    )
    return bundle.model_copy(update={"score_audit_records": [audit]}, deep=True)


def test_waiting_room_submit_is_not_marked_legacy_polluted() -> None:
    scoring = _pending_scoring()
    parked = _submit_run(
        status="awaiting_review",
        checkpoint="scoring_saved",
        feedback_id=None,
        report_id=None,
        state_version=None,
        previous_state_frozen=True,
    )

    assert inspect_legacy_score_posting(parked, scoring) is None


def test_completed_pending_submit_is_flagged_and_not_called_clean() -> None:
    scoring = _pending_scoring()
    flag = inspect_legacy_score_posting(_submit_run(), scoring)

    assert flag is not None
    assert flag.flag == "legacy_score_posted_before_review"
    assert flag.rebuildable is False
    assert flag.to_dict()["clean"] is False


def test_rebuild_fails_closed_without_frozen_baseline() -> None:
    scoring = _pending_scoring()
    flag = inspect_legacy_score_posting(_submit_run(), scoring)
    assert flag is not None

    with pytest.raises(DomainError) as raised:
        rebuild_legacy_score_posting(flag, apply=False)

    assert raised.value.code == "LEGACY_BASELINE_MISSING"


def test_rebuild_with_baseline_still_refuses_automatic_m5_rewind() -> None:
    scoring = _pending_scoring()
    flag = inspect_legacy_score_posting(
        _submit_run(previous_state_frozen=True),
        scoring,
    )
    assert flag is not None
    assert flag.rebuildable is True
    dry_run = rebuild_legacy_score_posting(flag, apply=False)
    assert dry_run.to_dict()["clean"] is False

    with pytest.raises(DomainError) as raised:
        rebuild_legacy_score_posting(flag, apply=True)

    assert raised.value.code == "LEGACY_REBUILD_NOT_AUTOMATIC"
