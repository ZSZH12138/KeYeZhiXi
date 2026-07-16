"""M0 platform-boundary contracts for the future Django shell."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


class ActorContext(ContractModel):
    """Pseudonymous actor scope produced by the M0 Django boundary."""

    actor_id: str = Field(min_length=1)
    role: Literal["student", "teacher", "course_admin", "system_admin"]
    course_ids: list[str]
    class_ids: list[str]
    issued_at: datetime

    def validate_business_rules(self) -> None:
        """Reject duplicated authorization scopes."""

        if len(self.course_ids) != len(set(self.course_ids)) or len(
            self.class_ids
        ) != len(set(self.class_ids)):
            raise DomainError(
                code="ACTOR_SCOPE_DUPLICATED",
                module="m0",
                message="actor course and class scopes must be unique",
            )


class AssessmentSubmission(ContractModel):
    """Path-free assessment form accepted by M0 and consumed by M8."""

    submission_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    answers: dict[str, str]
    submitted_at: datetime


class TeacherReviewSubmission(ContractModel):
    """Path-free teacher review form accepted by M0 and consumed by M9."""

    submission_id: str = Field(min_length=1)
    audit_id: str = Field(min_length=1)
    expected_audit_version: int = Field(ge=1)
    reviewer_id: str = Field(min_length=1)
    decision: Literal["approve", "override", "reject"]
    submitted_at: datetime


class AsyncJobStatus(ContractModel):
    """Portable status for a future M0 background job."""

    job_id: str = Field(min_length=1)
    job_type: Literal[
        "django_frontend",
        "vector_index",
        "llm_generation",
        "learning_model",
        "calibration",
    ]
    status: Literal["queued", "running", "succeeded", "failed", "skipped"]
    progress: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    result_ref: str | None = None
    error_code: str | None = None
    created_at: datetime
    finished_at: datetime | None = None

    def validate_business_rules(self) -> None:
        """Require terminal jobs to carry a completion timestamp."""

        terminal = self.status in {"succeeded", "failed", "skipped"}
        skipped_is_empty = self.status != "skipped" or (
            self.progress == 0.0
            and self.result_ref is None
            and self.error_code is None
        )
        if terminal != (self.finished_at is not None) or not skipped_is_empty:
            raise DomainError(
                code="JOB_TERMINAL_STATE_INVALID",
                module="m0",
                message="terminal time and skipped-job payload must be consistent",
            )
