"""M4 task-orchestration contract owned by 陈."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


_ASSESSMENT_TASK_TYPES = frozenset(
    {"diagnostic", "practice", "correction", "stage_assessment"}
)


class TaskPlan(ContractModel):
    """Frozen M4 routing decision consumed unchanged by M6 and M8."""

    task_id: str = Field(min_length=1)
    task_type: Literal[
        "qa", "diagnostic", "practice", "correction", "stage_assessment"
    ]
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    blueprint_id: str | None = Field(min_length=1)
    knowledge_bundle_id: str = Field(min_length=1)
    workflow: list[str] = Field(min_length=1)
    next_module: str = Field(min_length=1)
    created_at: datetime

    @field_validator("workflow")
    @classmethod
    def _validate_workflow_names(cls, value: list[str]) -> list[str]:
        if any(not module_name.strip() for module_name in value):
            raise ValueError("workflow module names must not be blank")
        return value

    def validate_business_rules(self) -> None:
        """Require an executable next step and a blueprint for assessments."""

        if len(self.workflow) != len(set(self.workflow)):
            raise DomainError(
                code="DUPLICATE_WORKFLOW_MODULE",
                module="m4",
                message="workflow module names must be unique",
                details={"task_id": self.task_id},
            )
        if self.next_module not in self.workflow:
            raise DomainError(
                code="NEXT_MODULE_NOT_ALLOWED",
                module="m4",
                message="next_module must be present in the frozen workflow",
                details={"next_module": self.next_module},
            )
        if self.requires_assessment() and self.blueprint_id is None:
            raise DomainError(
                code="BLUEPRINT_NOT_FOUND",
                module="m4",
                message="assessment tasks require a frozen blueprint",
                details={"task_type": self.task_type},
                recoverable=True,
            )

    def requires_assessment(self) -> bool:
        """Return whether the task must enter the M8 assessment workflow."""

        return self.task_type in _ASSESSMENT_TASK_TYPES

    def next_after(self, module_name: str) -> str | None:
        """Return the next frozen module, or ``None`` at workflow completion."""

        self.assert_module_allowed(module_name)
        index = self.workflow.index(module_name)
        if index + 1 == len(self.workflow):
            return None
        return self.workflow[index + 1]

    def assert_module_allowed(self, module_name: str) -> None:
        """Reject a module that is not part of this task's frozen workflow."""

        if module_name not in self.workflow:
            raise DomainError(
                code="MODULE_NOT_ALLOWED",
                module="m4",
                message="module is not part of the frozen task workflow",
                details={"module_name": module_name},
            )
