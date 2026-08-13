"""Private immutable dependency identity for restart-safe assessment work."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.modules.m0_platform.workflow import AssessmentRun
from course_insight.modules.m5_learner_class_state.update_policy import StatePolicy
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyExecutionRef,
)
from course_insight.modules.m9_teacher_analytics.suggestions import (
    TeacherThresholdPolicy,
)


@dataclass(frozen=True, slots=True)
class AssessmentDependencies:
    """Payload-free identity of every external input an operation executes."""

    knowledge_bundle_id: str
    knowledge_bundle_version: str
    knowledge_bundle_checksum: str
    course_package_id: str
    evidence_index_id: str | None
    evidence_index_version: str | None
    evidence_index_checksum: str | None
    state_policy_checksum: str | None
    teacher_policy_checksum: str | None

    def as_run_fields(self) -> dict[str, str | None]:
        """Return a fresh mapping accepted by private AssessmentRun metadata."""

        return {
            "knowledge_bundle_id": self.knowledge_bundle_id,
            "knowledge_bundle_version": self.knowledge_bundle_version,
            "knowledge_bundle_checksum": self.knowledge_bundle_checksum,
            "course_package_id": self.course_package_id,
            "evidence_index_id": self.evidence_index_id,
            "evidence_index_version": self.evidence_index_version,
            "evidence_index_checksum": self.evidence_index_checksum,
            "state_policy_checksum": self.state_policy_checksum,
            "teacher_policy_checksum": self.teacher_policy_checksum,
        }


def policy_execution_run_fields(
    execution: object,
) -> dict[str, str | None]:
    """Validate one prepared M6 binding and return its seven M0 fields."""

    if not isinstance(execution, PolicyExecutionRef):
        raise _dependency_mismatch(
            "prepared tutoring policy identity is invalid"
        )
    if execution.mode != "rules" and execution.artifact_sha256 is None:
        raise _dependency_mismatch(
            "learned tutoring policy identity has no artifact checksum"
        )
    return {
        "policy_id": execution.policy_id,
        "adapter_id": execution.adapter_id,
        "adapter_version": execution.adapter_version,
        "artifact_sha256": execution.artifact_sha256,
        "feature_schema_version": execution.feature_schema_version,
        "action_space_version": execution.action_space_version,
        "gate_policy_version": execution.gate_policy_version,
    }


def verify_policy_execution(
    run: AssessmentRun,
    execution: object,
) -> None:
    """Require a prepared binding to exactly match M0's frozen identity."""

    actual = policy_execution_run_fields(execution)
    expected = {
        field: getattr(run, field)
        for field in actual
    }
    if expected != actual:
        raise _dependency_mismatch(
            "prepared tutoring policy identity changed"
        )


def capture_assessment_dependencies(
    *,
    knowledge_bundle: KnowledgeBundle,
    evidence_index_ref: EvidenceIndexRef | None,
    state_policy_path: Path | None,
    teacher_policy_path: Path | None,
) -> AssessmentDependencies:
    """Validate and freeze exact dependency identities before recording a run."""

    if (
        evidence_index_ref is not None
        and evidence_index_ref.course_package_id
        != knowledge_bundle.course_package_id
    ):
        raise _dependency_mismatch(
            "knowledge bundle and evidence index do not share a course package"
        )
    if (state_policy_path is None) != (teacher_policy_path is None):
        raise _dependency_mismatch("assessment policy inputs are incomplete")

    state_checksum: str | None = None
    teacher_checksum: str | None = None
    if state_policy_path is not None and teacher_policy_path is not None:
        state_checksum = _validated_policy_checksum(
            state_policy_path,
            StatePolicy.from_bytes,
        )
        teacher_checksum = _validated_policy_checksum(
            teacher_policy_path,
            TeacherThresholdPolicy.from_bytes,
        )

    return AssessmentDependencies(
        knowledge_bundle_id=knowledge_bundle.knowledge_bundle_id,
        knowledge_bundle_version=knowledge_bundle.bundle_version,
        knowledge_bundle_checksum=knowledge_bundle.content_checksum(),
        course_package_id=knowledge_bundle.course_package_id,
        evidence_index_id=(
            None if evidence_index_ref is None else evidence_index_ref.index_id
        ),
        evidence_index_version=(
            None
            if evidence_index_ref is None
            else evidence_index_ref.index_version
        ),
        evidence_index_checksum=(
            None
            if evidence_index_ref is None
            else evidence_index_ref.content_checksum()
        ),
        state_policy_checksum=state_checksum,
        teacher_policy_checksum=teacher_checksum,
    )


def verify_policy_dependencies(
    dependencies: AssessmentDependencies,
    *,
    state_policy_path: Path,
    teacher_policy_path: Path,
) -> None:
    """Fail closed when policy files no longer match the recorded operation."""

    expected_state = dependencies.state_policy_checksum
    expected_teacher = dependencies.teacher_policy_checksum
    if expected_state is None or expected_teacher is None:
        raise _dependency_mismatch("assessment policy identity was not frozen")
    actual_state = _validated_policy_checksum(
        state_policy_path,
        StatePolicy.from_bytes,
    )
    actual_teacher = _validated_policy_checksum(
        teacher_policy_path,
        TeacherThresholdPolicy.from_bytes,
    )
    if not hmac.compare_digest(expected_state, actual_state) or not hmac.compare_digest(
        expected_teacher,
        actual_teacher,
    ):
        raise _dependency_mismatch("assessment policy content changed")


def _validated_policy_checksum(
    path: Path,
    validate: Callable[[bytes], object],
) -> str:
    try:
        payload = path.read_bytes()
    except (OSError, TypeError, ValueError) as error:
        raise DomainError(
            code="WORKFLOW_DEPENDENCY_UNAVAILABLE",
            module="application",
            message="assessment dependency is unavailable",
            recoverable=True,
        ) from error
    validate(payload)
    return hashlib.sha256(payload).hexdigest()


def _dependency_mismatch(message: str) -> DomainError:
    return DomainError(
        code="WORKFLOW_DEPENDENCY_MISMATCH",
        module="application",
        message=message,
        recoverable=True,
    )
