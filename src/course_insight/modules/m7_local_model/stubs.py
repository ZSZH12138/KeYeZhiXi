"""Deterministic zero-argument M7 service stub."""

from typing import Any
from datetime import datetime

from course_insight.contracts.intelligence import (
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.tutoring import StudentFeedbackPackage
from course_insight.modules.m7_local_model.adapter import PlaceholderRubricAdapter
from course_insight.modules.m7_local_model.repository import M7ModelAuditRecord
from course_insight.modules.m7_local_model.service import M7LocalModelService


class _MemoryM7Repository:
    def __init__(self) -> None:
        self.prompt_records: dict[str, dict[str, Any]] = {}
        self.feedback: dict[str, StudentFeedbackPackage] = {}
        self.invocation_audits: dict[str, ModelInvocationAudit] = {}
        self.safety_checks: dict[str, SafetyCheckResult] = {}
        self.execution_audits: dict[str, M7ModelAuditRecord] = {}

    def save_model_audit(
        self,
        audit_id: str,
        scoring_result: Any,
    ) -> None:
        del audit_id, scoring_result

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        self.prompt_records = {
            **self.prompt_records,
            prompt_id: dict(prompt_payload),
        }

    def save_generation_result(
        self,
        result: Any,
    ) -> None:
        del result

    def save_invocation_audit(
        self,
        audit: ModelInvocationAudit,
    ) -> None:
        self.invocation_audits = {
            **self.invocation_audits,
            audit.invocation_id: audit.model_copy(deep=True),
        }

    def save_safety_check(
        self,
        result: SafetyCheckResult,
    ) -> None:
        self.safety_checks = {
            **self.safety_checks,
            result.request_id: result.model_copy(deep=True),
        }

    def save_execution_audit(self, record: M7ModelAuditRecord) -> None:
        existing = self.execution_audits.get(record.invocation_id)
        request_match = next(
            (
                item
                for item in self.execution_audits.values()
                if item.request_id == record.request_id
            ),
            None,
        )
        authoritative = existing or request_match
        if authoritative is not None and authoritative != record:
            raise RuntimeError("M7 model audit identity conflict")
        self.execution_audits.setdefault(record.invocation_id, record)

    def get_execution_audit(
        self,
        invocation_id: str,
    ) -> M7ModelAuditRecord | None:
        return self.execution_audits.get(invocation_id)

    def purge_execution_audits_before(self, cutoff: datetime) -> int:
        expired = [
            invocation_id
            for invocation_id, record in self.execution_audits.items()
            if record.created_at < cutoff
        ]
        for invocation_id in expired:
            del self.execution_audits[invocation_id]
        return len(expired)

    def save_feedback(self, package: StudentFeedbackPackage) -> None:
        self.feedback = {
            **self.feedback,
            package.feedback_id: package.model_copy(deep=True),
        }

    def insert_or_get_feedback(
        self,
        package: StudentFeedbackPackage,
    ) -> StudentFeedbackPackage:
        existing = self.get_feedback_for_task(package.task_id, package.learner_id)
        if existing is None:
            self.save_feedback(package)
            existing = package
        if existing != package:
            raise RuntimeError("M7 feedback identity conflict")
        return existing.model_copy(deep=True)

    def get_feedback(
        self,
        feedback_id: str,
    ) -> StudentFeedbackPackage | None:
        package = self.feedback.get(feedback_id)
        return None if package is None else package.model_copy(deep=True)

    def get_feedback_for_task(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        for package in self.feedback.values():
            if package.task_id == task_id and package.learner_id == learner_id:
                return package.model_copy(deep=True)
        return None


class M7LocalModelServiceStub(M7LocalModelService):
    """Instantiate M7 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(
            PlaceholderRubricAdapter(),
            _MemoryM7Repository(),
            lambda _: True,
        )
