"""M7 persistence boundary for safe metadata and feedback recovery."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.intelligence import (
    LLMGenerationResult,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.tutoring import StudentFeedbackPackage


@runtime_checkable
class M7Repository(Protocol):
    """Persistence operations owned exclusively by M7."""

    def save_model_audit(
        self,
        audit_id: str,
        scoring_result: RubricScoringResult,
    ) -> None:
        """Legacy hook; governed M7 leaves final score auditing to M8."""

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        """Retain one validated prompt record for reproducibility."""

    def save_generation_result(
        self,
        result: LLMGenerationResult,
    ) -> None:
        """Legacy hook; governed M7 never sends model responses to storage."""

    def save_invocation_audit(
        self,
        audit: ModelInvocationAudit,
    ) -> None:
        """Retain privacy-safe model-call metadata when supported."""

    def save_safety_check(
        self,
        result: SafetyCheckResult,
    ) -> None:
        """Retain one post-generation safety decision when supported."""

    def save_feedback(self, package: StudentFeedbackPackage) -> None:
        """Retain one generated student-feedback package."""

    def insert_or_get_feedback(
        self,
        package: StudentFeedbackPackage,
    ) -> StudentFeedbackPackage:
        """Persist one feedback package or return its identical winner."""

    def get_feedback(
        self,
        feedback_id: str,
    ) -> StudentFeedbackPackage | None:
        """Load a generated package by stable feedback identity."""

    def get_feedback_for_task(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        """Load the package for one task and learner."""
