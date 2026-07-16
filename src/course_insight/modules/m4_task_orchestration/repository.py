"""M4 repository boundary for idempotent task plans."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.tasking import TaskPlan


_TASK_TABLE = "m4_task_plans"


class M4Repository(Protocol):
    """Persistence operations owned exclusively by M4."""

    def save_task_plan(self, plan: TaskPlan, idempotency_key: str) -> None:
        """Persist one task plan under a replay-safe key."""

    def get_task_plan(self, task_id: str) -> TaskPlan | None:
        """Load one task plan by stable identity."""
