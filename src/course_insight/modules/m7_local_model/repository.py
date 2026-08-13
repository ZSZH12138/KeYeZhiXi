"""M7 in-memory-capable boundary for model audits and prompt records."""

from __future__ import annotations

from typing import Any, Protocol

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.tutoring import StudentFeedbackPackage


class M7Repository(Protocol):
    """Audit and prompt persistence operations owned exclusively by M7."""

    def save_model_audit(
        self,
        audit_id: str,
        scoring_result: RubricScoringResult,
    ) -> None:
        """Retain one local-model scoring audit record."""

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        """Retain one validated prompt record for reproducibility."""

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
