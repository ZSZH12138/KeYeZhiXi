"""Regression tests for safe adoption of pre-v9 M0 workflow rows."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from course_insight.application.assessment_dependencies import (
    AssessmentDependencies,
)
from course_insight.application.assessment_recovery import AssessmentRecovery
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
)
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m0_platform.workflow import AssessmentRun
from tests.integration.test_postgres_m0_repository import (
    _Connection,
    _Pool,
    _Step,
)


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(seconds=1)


def _dependencies(operation: str) -> dict[str, object]:
    return {
        "knowledge_bundle_id": "bundle-1",
        "knowledge_bundle_version": "1.2.0",
        "knowledge_bundle_checksum": "a" * 64,
        "course_package_id": "package-1",
        "evidence_index_id": "index-1" if operation == "submit" else None,
        "evidence_index_version": "2026-07-25"
        if operation == "submit"
        else None,
        "evidence_index_checksum": "b" * 64
        if operation == "submit"
        else None,
        "state_policy_checksum": None if operation == "start" else "c" * 64,
        "teacher_policy_checksum": (
            None if operation == "start" else "d" * 64
        ),
        "previous_state_frozen": None if operation == "start" else False,
    }


def _run(operation: str = "submit", **updates: object) -> AssessmentRun:
    values: dict[str, object] = {
        "operation_id": f"{operation}:legacy-1",
        "operation": operation,
        "request_checksum": f"{operation}-request-checksum",
        "course_id": "course-1",
        "class_id": "class-1",
        "learner_id": "learner-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "paper_id": "paper-1",
        "attempt_id": None if operation == "start" else "attempt-1",
        "feedback_id": None,
        "report_id": None,
        "checkpoint": "pending",
        "status": "pending",
        "version": 4,
        "locked_by": None,
        "lease_until": None,
        "error_code": None,
        "created_at": NOW,
        "updated_at": LATER,
        "target_audit_id": "audit-1" if operation == "review" else None,
        "target_audit_version": 1 if operation == "review" else None,
        **_dependencies(operation),
    }
    values.update(updates)
    return AssessmentRun(**values)  # type: ignore[arg-type]


def _legacy(run: AssessmentRun, **updates: object) -> AssessmentRun:
    values = {
        "knowledge_bundle_id": None,
        "knowledge_bundle_version": None,
        "knowledge_bundle_checksum": None,
        "course_package_id": None,
        "evidence_index_id": None,
        "evidence_index_version": None,
        "evidence_index_checksum": None,
        "state_policy_checksum": None,
        "teacher_policy_checksum": None,
        "previous_state_frozen": None,
        "previous_learner_snapshot_id": None,
        "previous_learner_state_version": None,
        "previous_class_snapshot_id": None,
        "previous_class_state_version": None,
    }
    values.update(updates)
    return replace(run, **values)


def _policy_fields() -> dict[str, object]:
    return {
        "policy_id": "m6-deterministic-v1",
        "adapter_id": "m6-rules-adapter",
        "adapter_version": "v1",
        "artifact_sha256": None,
        "feature_schema_version": "m6-features-v1",
        "action_space_version": "m6-action-space-v1",
        "gate_policy_version": "m6-active-gate-v1",
    }


def _repository(path: Path) -> SQLiteM0Repository:
    repository = SQLiteM0Repository(path)
    repository.initialize()
    return repository


@pytest.mark.parametrize("operation", ["start", "submit", "review"])
def test_sqlite_replay_atomically_adopts_each_complete_legacy_shape(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = _repository(tmp_path / f"{operation}.db")
    candidate = _run(operation)
    legacy = _legacy(candidate, updated_at=NOW)
    repository.insert_or_get_assessment_run(legacy)

    adopted = repository.insert_or_get_assessment_run(candidate)
    stored = repository.get_assessment_run(candidate.operation_id)

    assert adopted == stored
    assert adopted.version == legacy.version + 1
    assert adopted.checkpoint == legacy.checkpoint
    assert adopted.status == legacy.status
    assert adopted.created_at == legacy.created_at
    for field, expected in _dependencies(operation).items():
        assert getattr(adopted, field) == expected

    replayed = repository.insert_or_get_assessment_run(candidate)
    assert replayed == adopted
    assert replayed.version == adopted.version


def test_recovery_adopts_only_start_knowledge_identity_before_submit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "start.db")
    start = _legacy(_run("start"), updated_at=NOW)
    repository.insert_or_get_assessment_run(start)
    service = M0PlatformService(
        tmp_path / "start.db",
        tmp_path / "runtime",
        tmp_path / "config",
        repository=repository,
    )
    recovery = AssessmentRecovery(m0=service, m5=object(), now=lambda: LATER)
    current = AssessmentDependencies(
        knowledge_bundle_id="bundle-1",
        knowledge_bundle_version="1.2.0",
        knowledge_bundle_checksum="a" * 64,
        course_package_id="package-1",
        evidence_index_id="index-current",
        evidence_index_version="2026-07-25",
        evidence_index_checksum="e" * 64,
        state_policy_checksum="f" * 64,
        teacher_policy_checksum="9" * 64,
    )

    adopted = recovery.adopt_legacy_dependencies(
        start,
        current,
        frozen_knowledge_bundle_id="bundle-1",
        frozen_course_package_id="package-1",
    )

    assert adopted.knowledge_bundle_id == "bundle-1"
    assert adopted.knowledge_bundle_checksum == "a" * 64
    assert adopted.evidence_index_id is None
    assert adopted.state_policy_checksum is None
    assert adopted.teacher_policy_checksum is None
    assert adopted.previous_state_frozen is None
    assert repository.get_assessment_run(start.operation_id) == adopted


def test_recovery_rejects_legacy_start_dependency_without_task_anchor(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "start-anchor.db")
    start = _legacy(_run("start"), updated_at=NOW)
    repository.insert_or_get_assessment_run(start)
    service = M0PlatformService(
        tmp_path / "start-anchor.db",
        tmp_path / "runtime",
        tmp_path / "config",
        repository=repository,
    )
    recovery = AssessmentRecovery(m0=service, m5=object(), now=lambda: LATER)
    current = AssessmentDependencies(
        knowledge_bundle_id="bundle-1",
        knowledge_bundle_version="1.2.0",
        knowledge_bundle_checksum="a" * 64,
        course_package_id="package-1",
        evidence_index_id=None,
        evidence_index_version=None,
        evidence_index_checksum=None,
        state_policy_checksum="f" * 64,
        teacher_policy_checksum="9" * 64,
    )

    with pytest.raises(DomainError) as captured:
        recovery.adopt_legacy_dependencies(
            start,
            current,
            frozen_knowledge_bundle_id="different-bundle",
            frozen_course_package_id="package-1",
        )

    assert captured.value.code == "WORKFLOW_DEPENDENCY_MISMATCH"
    assert repository.get_assessment_run(start.operation_id) == start


def test_sqlite_partial_legacy_state_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "partial.db")
    candidate = _run("submit")
    partial = _legacy(candidate, state_policy_checksum="e" * 64)
    repository.insert_or_get_assessment_run(partial)

    with pytest.raises(DomainError) as captured:
        repository.insert_or_get_assessment_run(candidate)

    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"
    assert repository.get_assessment_run(partial.operation_id) == partial


@pytest.mark.parametrize("operation", ["submit", "review"])
def test_sqlite_complete_dependencies_with_null_recovery_marker_fail_closed(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = _repository(tmp_path / f"partial-marker-{operation}.db")
    candidate = _run(operation)
    partial = replace(candidate, previous_state_frozen=None)
    repository.insert_or_get_assessment_run(partial)

    with pytest.raises(DomainError) as captured:
        repository.insert_or_get_assessment_run(candidate)

    expected = (
        "REVIEW_SUBMISSION_CONFLICT"
        if operation == "review"
        else "ASSESSMENT_SUBMISSION_CONFLICT"
    )
    assert captured.value.code == expected
    assert repository.get_assessment_run(partial.operation_id) == partial


def test_sqlite_legacy_adoption_rejects_changed_immutable_request(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "conflict.db")
    candidate = _run("review")
    legacy = _legacy(candidate)
    repository.insert_or_get_assessment_run(legacy)

    with pytest.raises(DomainError) as captured:
        repository.adopt_legacy_assessment_run(
            replace(candidate, request_checksum="changed-request")
        )

    assert captured.value.code == "REVIEW_SUBMISSION_CONFLICT"
    assert repository.get_assessment_run(legacy.operation_id) == legacy


def test_sqlite_legacy_adoption_rejects_incomplete_operation_shape(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "shape.db")
    complete = _run("submit")
    legacy = _legacy(complete)
    repository.insert_or_get_assessment_run(legacy)
    incomplete = replace(
        complete,
        evidence_index_id=None,
        evidence_index_version=None,
        evidence_index_checksum=None,
    )

    with pytest.raises(DomainError) as captured:
        repository.adopt_legacy_assessment_run(incomplete)

    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"
    assert repository.get_assessment_run(legacy.operation_id) == legacy


def test_sqlite_legacy_adoption_rejects_post_v8_checkpoint(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "checkpoint.db")
    candidate = _run("submit")
    impossible = _legacy(
        candidate,
        checkpoint="state_inputs_frozen",
        status="failed",
    )
    repository.insert_or_get_assessment_run(impossible)

    with pytest.raises(DomainError) as captured:
        repository.adopt_legacy_assessment_run(candidate)

    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"
    assert repository.get_assessment_run(impossible.operation_id) == impossible


def test_sqlite_legacy_post_state_checkpoint_adopts_as_recovery_complete(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "post-state.db")
    candidate = _run("submit")
    legacy = _legacy(
        candidate,
        checkpoint="state_saved",
        status="failed",
        state_version=7,
    )
    repository.insert_or_get_assessment_run(legacy)

    adopted = repository.insert_or_get_assessment_run(candidate)

    assert adopted.checkpoint == "state_saved"
    assert adopted.state_version == 7
    assert adopted.previous_state_frozen is True


def test_sqlite_legacy_post_state_checkpoint_requires_exact_state_reference(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "post-state-invalid.db")
    candidate = _run("review")
    corrupt = _legacy(
        candidate,
        checkpoint="analytics_saved",
        status="failed",
        state_version=None,
    )
    repository.insert_or_get_assessment_run(corrupt)

    with pytest.raises(DomainError) as captured:
        repository.insert_or_get_assessment_run(candidate)

    assert captured.value.code == "REVIEW_SUBMISSION_CONFLICT"
    assert repository.get_assessment_run(corrupt.operation_id) == corrupt


def test_postgres_legacy_adoption_locks_and_cas_updates_only_all_null_row() -> None:
    candidate = _run("submit")
    legacy = _legacy(candidate, updated_at=NOW)
    adopted = replace(
        candidate,
        version=legacy.version + 1,
        created_at=legacy.created_at,
        updated_at=candidate.updated_at,
    )
    connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_mapping(legacy),
            ),
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "version = %s",
                    "WHERE operation_id = %s",
                    "AND version = %s",
                    "knowledge_bundle_id IS NULL",
                    "previous_state_frozen IS NULL",
                    "RETURNING operation_id",
                ),
                one=_mapping(adopted),
                rowcount=1,
            ),
        ]
    )

    result = PostgresM0Repository(_Pool(connection)).adopt_legacy_assessment_run(
        candidate
    )

    assert result == adopted


def test_postgres_narrowly_cas_adopts_all_null_v10_policy_identity() -> None:
    candidate = _run(
        "submit",
        checkpoint="tutoring_saved",
        status="running",
        state_version=3,
        previous_state_frozen=True,
        locked_by="worker-1",
        lease_until=NOW + timedelta(seconds=30),
        **_policy_fields(),
    )
    legacy = replace(
        candidate,
        policy_id=None,
        adapter_id=None,
        adapter_version=None,
        artifact_sha256=None,
        feature_schema_version=None,
        action_space_version=None,
        gate_policy_version=None,
    )
    adopted = replace(candidate, version=legacy.version + 1)
    connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_mapping(legacy),
            ),
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "SET policy_id = %s",
                    "gate_policy_version = %s",
                    "policy_id IS NULL",
                    "RETURNING operation_id",
                ),
                one=_mapping(adopted),
                rowcount=1,
            ),
        ]
    )

    result = PostgresM0Repository(_Pool(connection)).adopt_legacy_assessment_run(
        candidate
    )

    assert result == adopted


def test_policy_frozen_checkpoint_cannot_be_adopted_without_identity(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "policy-frozen-corrupt.db")
    candidate = _run(
        "submit",
        checkpoint="policy_frozen",
        status="failed",
        state_version=3,
        previous_state_frozen=True,
        **_policy_fields(),
    )
    corrupt = replace(
        candidate,
        policy_id=None,
        adapter_id=None,
        adapter_version=None,
        artifact_sha256=None,
        feature_schema_version=None,
        action_space_version=None,
        gate_policy_version=None,
    )
    repository.insert_or_get_assessment_run(corrupt)

    with pytest.raises(DomainError) as captured:
        repository.adopt_legacy_assessment_run(candidate)

    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"
    assert repository.get_assessment_run(corrupt.operation_id) == corrupt


def _mapping(run: AssessmentRun) -> dict[str, object]:
    return {
        field: getattr(run, field)
        for field in run.__dataclass_fields__
    }
