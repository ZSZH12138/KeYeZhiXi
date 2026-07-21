"""Deterministic zero-argument M4 service stub."""

from datetime import datetime, timedelta, timezone

from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.repository import M4Repository
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)


class _StubM4Repository:
    """Per-service memory stub; formal persistence is provided by SQLite."""

    def __init__(self) -> None:
        self._plans_by_key: dict[str, TaskPlan] = {}

    def insert_or_get_task_plan(
        self,
        plan: TaskPlan,
        idempotency_key: str,
    ) -> TaskPlan:
        existing = self._plans_by_key.get(idempotency_key)
        if existing is not None:
            return existing.model_copy(deep=True)
        stored = plan.model_copy(deep=True)
        self._plans_by_key = {**self._plans_by_key, idempotency_key: stored}
        return stored.model_copy(deep=True)

    def save_task_plan(self, plan: TaskPlan, idempotency_key: str) -> None:
        self.insert_or_get_task_plan(plan, idempotency_key)

    def get_task_plan(self, task_id: str) -> TaskPlan | None:
        for plan in self._plans_by_key.values():
            if plan.task_id == task_id:
                return plan.model_copy(deep=True)
        return None


class M4TaskOrchestrationServiceStub(M4TaskOrchestrationService):
    """Instantiate M4 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        repository: M4Repository = _StubM4Repository()
        super().__init__(repository, canonical_idempotency_key)

    def _created_at(self) -> datetime:
        return datetime(
            2026,
            7,
            15,
            9,
            0,
            tzinfo=timezone(timedelta(hours=8)),
        )
