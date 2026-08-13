"""Private, payload-free assessment workflow state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from course_insight.contracts.errors import DomainError


WorkflowOperation = Literal["start", "submit", "review"]
WorkflowStatus = Literal["pending", "running", "failed", "completed"]

_CHECKPOINTS: dict[WorkflowOperation, tuple[str, ...]] = {
    "start": ("pending", "claimed", "task_saved", "paper_saved", "completed"),
    "submit": (
        "pending",
        "claimed",
        "scoring_saved",
        "events_appended",
        "state_inputs_frozen",
        "state_saved",
        "policy_frozen",
        "tutoring_saved",
        "feedback_saved",
        "analytics_saved",
        "completed",
    ),
    "review": (
        "pending",
        "claimed",
        "decision_saved",
        "review_saved",
        "events_appended",
        "state_inputs_frozen",
        "state_saved",
        "analytics_saved",
        "completed",
    ),
}
_REPLAY_IDENTITY_FIELDS = (
    "operation_id",
    "operation",
    "request_checksum",
    "course_id",
    "class_id",
    "learner_id",
    "session_id",
    "task_id",
    "paper_id",
    "attempt_id",
    "target_audit_id",
    "target_audit_version",
)
_DEPENDENCY_FIELDS = (
    "knowledge_bundle_id",
    "knowledge_bundle_version",
    "knowledge_bundle_checksum",
    "course_package_id",
    "evidence_index_id",
    "evidence_index_version",
    "evidence_index_checksum",
    "state_policy_checksum",
    "teacher_policy_checksum",
)
_LEGACY_RECOVERY_FIELDS = (
    *_DEPENDENCY_FIELDS,
    "previous_state_frozen",
    "previous_learner_snapshot_id",
    "previous_learner_state_version",
    "previous_class_snapshot_id",
    "previous_class_state_version",
)
_POLICY_FIELDS = (
    "policy_id",
    "adapter_id",
    "adapter_version",
    "artifact_sha256",
    "feature_schema_version",
    "action_space_version",
    "gate_policy_version",
)
_LEGACY_POLICY_ADOPTION_CHECKPOINTS = frozenset(
    {
        "tutoring_saved",
        "feedback_saved",
        "analytics_saved",
    }
)
_POLICY_BOUND_CHECKPOINTS = frozenset(
    {
        "policy_frozen",
        "tutoring_saved",
        "feedback_saved",
        "analytics_saved",
        "completed",
    }
)
_POST_STATE_CHECKPOINTS = {
    "submit": frozenset(
        {
            "state_saved",
            "policy_frozen",
            "tutoring_saved",
            "feedback_saved",
            "analytics_saved",
            "completed",
        }
    ),
    "review": frozenset({"state_saved", "analytics_saved", "completed"}),
}


@dataclass(frozen=True, slots=True)
class AssessmentRun:
    """M0-owned outer workflow metadata; never a domain payload container."""

    operation_id: str
    operation: WorkflowOperation
    request_checksum: str
    course_id: str
    class_id: str
    learner_id: str
    session_id: str
    task_id: str
    paper_id: str
    attempt_id: str | None
    feedback_id: str | None
    report_id: str | None
    checkpoint: str
    status: WorkflowStatus
    version: int
    locked_by: str | None
    lease_until: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    scoring_result_checksum: str | None = None
    target_audit_id: str | None = None
    target_audit_version: int | None = None
    state_version: int | None = None
    knowledge_bundle_id: str | None = None
    knowledge_bundle_version: str | None = None
    knowledge_bundle_checksum: str | None = None
    course_package_id: str | None = None
    evidence_index_id: str | None = None
    evidence_index_version: str | None = None
    evidence_index_checksum: str | None = None
    state_policy_checksum: str | None = None
    teacher_policy_checksum: str | None = None
    previous_state_frozen: bool | None = None
    previous_learner_snapshot_id: str | None = None
    previous_learner_state_version: int | None = None
    previous_class_snapshot_id: str | None = None
    previous_class_state_version: int | None = None
    policy_id: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    artifact_sha256: str | None = None
    feature_schema_version: str | None = None
    action_space_version: str | None = None
    gate_policy_version: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.operation_id,
            self.request_checksum,
            self.course_id,
            self.class_id,
            self.learner_id,
            self.session_id,
            self.task_id,
            self.paper_id,
        )
        checksum_invalid = any(
            checksum is not None and not _is_sha256(checksum)
            for checksum in (
                self.scoring_result_checksum,
                self.knowledge_bundle_checksum,
                self.evidence_index_checksum,
                self.state_policy_checksum,
                self.teacher_policy_checksum,
                self.artifact_sha256,
            )
        )
        knowledge_identity = (
            self.knowledge_bundle_id,
            self.knowledge_bundle_version,
            self.knowledge_bundle_checksum,
            self.course_package_id,
        )
        evidence_identity = (
            self.evidence_index_id,
            self.evidence_index_version,
            self.evidence_index_checksum,
        )
        dependency_identity_invalid = (
            not _all_or_none(knowledge_identity)
            or not _all_or_none(evidence_identity)
            or any(
                value is not None and not value
                for value in (
                    *knowledge_identity,
                    *evidence_identity,
                )
            )
        )
        policy_identity = (
            self.policy_id,
            self.adapter_id,
            self.adapter_version,
            self.feature_schema_version,
            self.action_space_version,
            self.gate_policy_version,
        )
        policy_identity_invalid = (
            not _all_or_none(policy_identity)
            or self.artifact_sha256 is not None
            and all(value is None for value in policy_identity)
            or any(value is not None for value in policy_identity)
            and (
                self.operation != "submit"
                or self.checkpoint not in _POLICY_BOUND_CHECKPOINTS
                or self.state_version is None
                or self.previous_state_frozen is not True
            )
            or any(
                value is not None and not value
                for value in policy_identity
            )
        )
        learner_pair_invalid = (
            self.previous_learner_snapshot_id is None
        ) != (self.previous_learner_state_version is None)
        class_reference_invalid = (
            self.previous_class_snapshot_id is None
            and self.previous_class_state_version is not None
        )
        frozen_references = (
            self.previous_learner_snapshot_id,
            self.previous_learner_state_version,
            self.previous_class_snapshot_id,
            self.previous_class_state_version,
        )
        frozen_state_invalid = (
            type(self.previous_state_frozen) not in {bool, type(None)}
            or self.previous_state_frozen is not True
            and any(value is not None for value in frozen_references)
            or learner_pair_invalid
            or class_reference_invalid
            or any(
                value is not None and not value
                for value in (
                    self.previous_learner_snapshot_id,
                    self.previous_class_snapshot_id,
                )
            )
        )
        target_pair_invalid = (self.target_audit_id is None) != (
            self.target_audit_version is None
        )
        completed_refs_missing = self.status == "completed" and (
            self.operation in {"submit", "review"}
            and (
                self.scoring_result_checksum is None
                or self.state_version is None
                or self.report_id is None
            )
            or self.operation == "submit" and self.feedback_id is None
        )
        if (
            any(not value for value in required)
            or self.version < 1
            or checksum_invalid
            or dependency_identity_invalid
            or policy_identity_invalid
            or frozen_state_invalid
            or target_pair_invalid
            or self.operation == "review"
            and self.target_audit_id is None
            or self.operation != "review"
            and self.target_audit_id is not None
            or completed_refs_missing
            or self.target_audit_version is not None
            and self.target_audit_version < 1
            or self.state_version is not None
            and self.state_version < 1
            or self.previous_learner_state_version is not None
            and self.previous_learner_state_version < 1
            or self.previous_class_state_version is not None
            and self.previous_class_state_version < 1
            or self.checkpoint not in _CHECKPOINTS[self.operation]
            or (self.locked_by is None) != (self.lease_until is None)
        ):
            raise ValueError("assessment workflow metadata is invalid")


def _is_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _all_or_none(values: tuple[object | None, ...]) -> bool:
    return all(value is None for value in values) or all(
        value is not None for value in values
    )


def reconcile_legacy_assessment_run(
    current: AssessmentRun,
    candidate: AssessmentRun,
) -> AssessmentRun:
    """Return an exact replay or a safely adopted pre-v9 workflow row.

    Adoption is deliberately narrow: the old replay identity must still match,
    every v9 dependency/recovery field must be NULL, and the candidate must
    carry the complete dependency shape required by its operation.
    """

    _require_replay_identity(current, candidate)
    current_dependencies = _field_values(current, _DEPENDENCY_FIELDS)
    candidate_dependencies = _field_values(candidate, _DEPENDENCY_FIELDS)
    resolved = current
    if current_dependencies == candidate_dependencies:
        recovery_marker_invalid = (
            current.previous_state_frozen is not None
            if current.operation == "start"
            else (
                current.previous_state_frozen is None
                and any(
                    value is not None
                    for value in current_dependencies
                )
            )
        )
        if recovery_marker_invalid:
            raise _replay_conflict(current.operation)
    else:
        if (
            any(
                value is not None
                for value in _field_values(current, _LEGACY_RECOVERY_FIELDS)
            )
            or not _valid_adoption_candidate(candidate)
            or current.checkpoint == "state_inputs_frozen"
        ):
            raise _replay_conflict(current.operation)

        previous_state_frozen: bool | None = None
        if current.operation != "start":
            post_state = current.checkpoint in _POST_STATE_CHECKPOINTS[
                current.operation
            ]
            if post_state and current.state_version is None:
                raise _replay_conflict(current.operation)
            previous_state_frozen = post_state

        resolved = replace(
            current,
            knowledge_bundle_id=candidate.knowledge_bundle_id,
            knowledge_bundle_version=candidate.knowledge_bundle_version,
            knowledge_bundle_checksum=candidate.knowledge_bundle_checksum,
            course_package_id=candidate.course_package_id,
            evidence_index_id=candidate.evidence_index_id,
            evidence_index_version=candidate.evidence_index_version,
            evidence_index_checksum=candidate.evidence_index_checksum,
            state_policy_checksum=candidate.state_policy_checksum,
            teacher_policy_checksum=candidate.teacher_policy_checksum,
            previous_state_frozen=previous_state_frozen,
            version=current.version + 1,
            updated_at=max(current.updated_at, candidate.updated_at),
        )
    return _reconcile_legacy_policy_identity(resolved, candidate)


def assert_assessment_run_replay(
    current: AssessmentRun,
    candidate: AssessmentRun,
) -> None:
    """Reject any replay that changes its v8 identity or frozen dependencies."""

    _require_replay_identity(current, candidate)
    if _field_values(current, _DEPENDENCY_FIELDS) != _field_values(
        candidate,
        _DEPENDENCY_FIELDS,
    ):
        raise _replay_conflict(current.operation)
    candidate_policy = _field_values(candidate, _POLICY_FIELDS)
    if (
        any(value is not None for value in candidate_policy)
        and _field_values(current, _POLICY_FIELDS) != candidate_policy
    ):
        raise _replay_conflict(current.operation)


def _reconcile_legacy_policy_identity(
    current: AssessmentRun,
    candidate: AssessmentRun,
) -> AssessmentRun:
    current_policy = _field_values(current, _POLICY_FIELDS)
    candidate_policy = _field_values(candidate, _POLICY_FIELDS)
    if all(value is None for value in candidate_policy):
        return current
    if current_policy == candidate_policy:
        return current
    if (
        any(value is not None for value in current_policy)
        or current.operation != "submit"
        or current.checkpoint not in _LEGACY_POLICY_ADOPTION_CHECKPOINTS
        or current.state_version is None
        or current.previous_state_frozen is not True
    ):
        raise _replay_conflict(current.operation)
    return replace(
        current,
        policy_id=candidate.policy_id,
        adapter_id=candidate.adapter_id,
        adapter_version=candidate.adapter_version,
        artifact_sha256=candidate.artifact_sha256,
        feature_schema_version=candidate.feature_schema_version,
        action_space_version=candidate.action_space_version,
        gate_policy_version=candidate.gate_policy_version,
        version=current.version + 1,
        updated_at=max(current.updated_at, candidate.updated_at),
    )


def _require_replay_identity(
    current: AssessmentRun,
    candidate: AssessmentRun,
) -> None:
    if _field_values(current, _REPLAY_IDENTITY_FIELDS) != _field_values(
        candidate,
        _REPLAY_IDENTITY_FIELDS,
    ):
        raise _replay_conflict(current.operation)


def _valid_adoption_candidate(candidate: AssessmentRun) -> bool:
    knowledge = (
        candidate.knowledge_bundle_id,
        candidate.knowledge_bundle_version,
        candidate.knowledge_bundle_checksum,
        candidate.course_package_id,
    )
    evidence = (
        candidate.evidence_index_id,
        candidate.evidence_index_version,
        candidate.evidence_index_checksum,
    )
    baseline_refs = (
        candidate.previous_learner_snapshot_id,
        candidate.previous_learner_state_version,
        candidate.previous_class_snapshot_id,
        candidate.previous_class_state_version,
    )
    if any(value is None for value in knowledge) or any(
        value is not None for value in baseline_refs
    ):
        return False
    if candidate.operation == "start":
        return (
            all(value is None for value in evidence)
            and candidate.state_policy_checksum is None
            and candidate.teacher_policy_checksum is None
            and candidate.previous_state_frozen is None
        )
    if (
        candidate.state_policy_checksum is None
        or candidate.teacher_policy_checksum is None
        or candidate.previous_state_frozen is not False
    ):
        return False
    return (
        all(value is not None for value in evidence)
        if candidate.operation == "submit"
        else all(value is None for value in evidence)
    )


def _field_values(
    run: AssessmentRun,
    fields: tuple[str, ...],
) -> tuple[object, ...]:
    return tuple(getattr(run, field) for field in fields)


def _replay_conflict(operation: WorkflowOperation) -> DomainError:
    return DomainError(
        code=(
            "REVIEW_SUBMISSION_CONFLICT"
            if operation == "review"
            else "ASSESSMENT_SUBMISSION_CONFLICT"
        ),
        module="m0",
        message="assessment workflow replay payload does not match",
        recoverable=True,
    )


def advance_run(
    run: AssessmentRun,
    checkpoint: str,
    *,
    now: datetime,
    **updates: object,
) -> AssessmentRun:
    """Return the next legal immutable workflow state or fail closed."""

    checkpoints = _CHECKPOINTS[run.operation]
    current_index = checkpoints.index(run.checkpoint)
    expected = (
        checkpoints[current_index + 1]
        if current_index + 1 < len(checkpoints)
        else None
    )
    if checkpoint != expected or run.status == "completed":
        raise DomainError(
            code="WORKFLOW_TRANSITION_INVALID",
            module="m0",
            message="assessment workflow checkpoint transition is not allowed",
            details={
                "operation": run.operation,
                "checkpoint": run.checkpoint,
            },
            recoverable=True,
        )
    status: WorkflowStatus = (
        "completed" if checkpoint == "completed" else "running"
    )
    return replace(
        run,
        checkpoint=checkpoint,
        status=status,
        version=run.version + 1,
        updated_at=now,
        **updates,
    )


def next_checkpoint(run: AssessmentRun) -> str | None:
    """Return the only legal next checkpoint."""

    checkpoints = _CHECKPOINTS[run.operation]
    index = checkpoints.index(run.checkpoint)
    return checkpoints[index + 1] if index + 1 < len(checkpoints) else None
