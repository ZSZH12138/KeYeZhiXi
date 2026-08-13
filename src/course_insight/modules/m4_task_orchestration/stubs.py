"""Deterministic zero-argument M4 service stub."""

from datetime import datetime, timedelta, timezone
from threading import Lock

from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.intent_service import (
    M4IntentService,
    StoredIntentDecision,
)
from course_insight.modules.m4_task_orchestration.repository import M4Repository
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)


class _StubM4Repository:
    """Per-service memory stub; formal persistence is provided by SQLite."""

    def __init__(self) -> None:
        self._plans_by_key: dict[str, TaskPlan] = {}
        self._intent_decisions: dict[str, StoredIntentDecision] = {}
        self._lock = Lock()

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

    def get_intent_decision(
        self,
        request_key: str,
    ) -> StoredIntentDecision | None:
        with self._lock:
            return self._intent_decisions.get(request_key)

    def insert_or_get_intent_decision(
        self,
        decision: StoredIntentDecision,
    ) -> StoredIntentDecision:
        with self._lock:
            existing = self._intent_decisions.get(decision.request_key)
            if existing is not None:
                return existing
            self._intent_decisions = {
                **self._intent_decisions,
                decision.request_key: decision,
            }
            return decision


class M4TaskOrchestrationServiceStub(M4TaskOrchestrationService):
    """Instantiate M4 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        repository = _StubM4Repository()
        intent_service = M4IntentService(
            repository,
            canonical_idempotency_key,
        )
        typed_repository: M4Repository = repository
        super().__init__(
            typed_repository,
            canonical_idempotency_key,
            intent_service=intent_service,
        )

    def _created_at(self) -> datetime:
        return datetime(
            2026,
            7,
            15,
            9,
            0,
            tzinfo=timezone(timedelta(hours=8)),
        )
