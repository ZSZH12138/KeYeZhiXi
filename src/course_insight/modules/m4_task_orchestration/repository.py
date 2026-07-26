"""M4 repository boundary for idempotent task plans."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from course_insight.contracts.tasking import TaskPlan

if TYPE_CHECKING:
    from course_insight.modules.m4_task_orchestration.intent_service import (
        StoredIntentDecision,
    )


_TASK_TABLE = "m4_task_plans"


class M4Repository(Protocol):
    """Persistence operations owned exclusively by M4."""

    def insert_or_get_task_plan(
        self,
        plan: TaskPlan,
        idempotency_key: str,
    ) -> TaskPlan:
        """Atomically insert a plan or return the existing replay winner."""

    def save_task_plan(self, plan: TaskPlan, idempotency_key: str) -> None:
        """Persist one task plan under a replay-safe key."""

    def get_task_plan(self, task_id: str) -> TaskPlan | None:
        """Load one task plan by stable identity."""

    def get_intent_decision(
        self,
        request_key: str,
    ) -> StoredIntentDecision | None:
        """Load the first private intent decision for an exact request key."""

    def insert_or_get_intent_decision(
        self,
        decision: StoredIntentDecision,
    ) -> StoredIntentDecision:
        """Atomically insert a decision or return the first-writer winner."""
