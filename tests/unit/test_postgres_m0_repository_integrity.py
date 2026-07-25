"""Fail-closed row decoding checks for PostgreSQL M0."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.infrastructure.postgresql import migration_runner
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
)
from course_insight.infrastructure.postgresql.m0_repository import _run_from_row
from course_insight.modules.m0_platform.workflow import AssessmentRun


NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _pending_run() -> AssessmentRun:
    return AssessmentRun(
        operation_id="submit-integrity",
        operation="submit",
        request_checksum="nonempty-checksum-contract",
        course_id="course-1",
        class_id="class-1",
        learner_id="learner-1",
        session_id="session-1",
        task_id="task-1",
        paper_id="paper-1",
        attempt_id="attempt-1",
        feedback_id=None,
        report_id=None,
        checkpoint="pending",
        status="pending",
        version=1,
        locked_by=None,
        lease_until=None,
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_workflow_decoder_rejects_status_outside_domain_state_machine() -> None:
    run = _pending_run()
    row = {
        field: getattr(run, field)
        for field in run.__dataclass_fields__
    }
    row["status"] = "invented"

    with pytest.raises(ValueError, match="status"):
        _run_from_row(row)


def test_schema_health_uses_migration_checksum_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = object()
    monkeypatch.setattr(
        migration_runner,
        "current_schema_version",
        lambda candidate: 7,
    )
    monkeypatch.setattr(migration_runner, "SCHEMA_VERSION", 7)
    monkeypatch.setattr(
        migration_runner,
        "schema_is_current",
        lambda candidate: candidate is not pool,
    )

    assert PostgresM0Repository(pool).schema_is_current() is False


def test_private_reference_column_is_runtime_allowlisted() -> None:
    repository = PostgresM0Repository(object())

    with pytest.raises(ValueError, match="reference column"):
        repository._get_assessment_run_by_reference(
            "paper_id; DROP TABLE m0_assessment_runs; --",  # type: ignore[arg-type]
            "paper-1",
            operation=None,
            status=None,
        )
