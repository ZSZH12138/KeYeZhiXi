"""Deterministic zero-argument M7 service stub."""

from typing import Any

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.tutoring import StudentFeedbackPackage
from course_insight.modules.m7_local_model.adapter import PlaceholderRubricAdapter
from course_insight.modules.m7_local_model.service import M7LocalModelService


class _MemoryM7Repository:
    def __init__(self) -> None:
        self.model_audits: dict[str, RubricScoringResult] = {}
        self.prompt_records: dict[str, dict[str, Any]] = {}
        self.feedback: dict[str, StudentFeedbackPackage] = {}

    def save_model_audit(
        self,
        audit_id: str,
        scoring_result: RubricScoringResult,
    ) -> None:
        self.model_audits = {
            **self.model_audits,
            audit_id: scoring_result.model_copy(deep=True),
        }

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        self.prompt_records = {
            **self.prompt_records,
            prompt_id: dict(prompt_payload),
        }

    def save_feedback(self, package: StudentFeedbackPackage) -> None:
        self.feedback = {
            **self.feedback,
            package.feedback_id: package.model_copy(deep=True),
        }


class M7LocalModelServiceStub(M7LocalModelService):
    """Instantiate M7 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(PlaceholderRubricAdapter(), _MemoryM7Repository(), lambda _: True)
