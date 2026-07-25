"""Formal M4 task-orchestration service boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from types import MappingProxyType

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.state import LearnerStateSnapshot
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m4_task_orchestration.repository import M4Repository
from course_insight.modules.m4_task_orchestration.routing import (
    resolve_blueprint_id,
    resolve_task_type,
    workflow_for,
)


IdempotencyKeyFactory = Callable[[Mapping[str, str | None]], str]


class M4TaskOrchestrationService:
    """Convert learner intent into an idempotent module workflow."""

    def __init__(
        self,
        repository: M4Repository,
        idempotency_key_factory: IdempotencyKeyFactory,
        *,
        blueprint_by_task_type: Mapping[str, str] | None = None,
    ) -> None:
        self._repository = repository
        self._idempotency_key_factory = idempotency_key_factory
        self._blueprint_by_task_type = MappingProxyType(
            dict(blueprint_by_task_type or {})
        )

    def create_task_plan(
        self,
        student_text: str,
        task_type_hint: str | None,
        course_id: str,
        class_id: str,
        learner_id: str,
        session_id: str,
        knowledge_bundle: KnowledgeBundle,
        learner_state_snapshot: LearnerStateSnapshot | None,
    ) -> TaskPlan:
        """Create a frozen task plan from raw learner intent.

        原始输入：学生文本、可选类型提示、四个身份和知识/状态对象。
        契约来源：原始学生请求、M3 知识包和可选 M5 状态。
        返回消费者：M6 教学控制和 M8 个性化测评。
        业务校验：任务类型、蓝图、工作流和幂等身份必须一致。
        错误码：UNSUPPORTED_TASK。
        """

        task_type = resolve_task_type(student_text, task_type_hint)
        self._validate_inputs(
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            knowledge_bundle=knowledge_bundle,
            learner_state_snapshot=learner_state_snapshot,
        )
        blueprint_id = resolve_blueprint_id(
            task_type=task_type,
            course_id=course_id,
            knowledge_bundle=knowledge_bundle,
            blueprint_by_task_type=self._blueprint_by_task_type,
        )
        identity = {
            "course_id": course_id,
            "class_id": class_id,
            "learner_id": learner_id,
            "session_id": session_id,
            "task_type": task_type,
            "knowledge_bundle_id": knowledge_bundle.knowledge_bundle_id,
            "course_package_id": knowledge_bundle.course_package_id,
            "blueprint_id": blueprint_id,
        }
        idempotency_key = self._idempotency_key_factory(identity)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise DomainError(
                code="UNSUPPORTED_TASK",
                module="m4",
                message="idempotency key factory returned an invalid key",
            )
        task_id = f"task_{idempotency_key}"
        workflow = workflow_for(task_type)
        plan = TaskPlan(
            task_id=task_id,
            task_type=task_type,
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            blueprint_id=blueprint_id,
            knowledge_bundle_id=knowledge_bundle.knowledge_bundle_id,
            course_package_id=knowledge_bundle.course_package_id,
            workflow=workflow,
            next_module=workflow[0],
            created_at=self._created_at(),
        )
        authoritative = self._repository.insert_or_get_task_plan(
            plan.model_copy(deep=True),
            idempotency_key,
        )
        self._assert_authoritative_plan(plan, authoritative)
        return authoritative.model_copy(deep=True)

    def get_task_plan(self, task_id: str) -> TaskPlan | None:
        """Recover a previously created task plan by stable identity."""

        getter = getattr(self._repository, "get_task_plan", None)
        if not callable(getter):
            return None
        plan = getter(task_id)
        if plan is None:
            return None
        if not isinstance(plan, TaskPlan) or plan.task_id != task_id:
            raise RuntimeError("M4 persisted task plan is corrupt")
        return plan.model_copy(deep=True)

    def _created_at(self) -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _assert_authoritative_plan(
        candidate: TaskPlan,
        authoritative: TaskPlan,
    ) -> None:
        if not isinstance(authoritative, TaskPlan) or authoritative.model_dump(
            exclude={"created_at"},
        ) != candidate.model_dump(exclude={"created_at"}):
            raise RuntimeError(
                "M4 idempotency collision or persisted task corruption"
            )

    @staticmethod
    def _validate_inputs(
        *,
        course_id: str,
        class_id: str,
        learner_id: str,
        session_id: str,
        knowledge_bundle: KnowledgeBundle,
        learner_state_snapshot: LearnerStateSnapshot | None,
    ) -> None:
        if any(
            not value.strip()
            for value in (course_id, class_id, learner_id, session_id)
        ):
            raise DomainError(
                code="UNSUPPORTED_TASK",
                module="m4",
                message="task identity fields must not be blank",
            )
        if (
            knowledge_bundle.course_id != course_id
            or knowledge_bundle.status != "published"
            or not knowledge_bundle.knowledge_bundle_id.strip()
            or not knowledge_bundle.course_package_id.strip()
        ):
            raise DomainError(
                code="KNOWLEDGE_BUNDLE_MISMATCH",
                module="m4",
                message="task and published knowledge bundle must share a course",
            )
        if learner_state_snapshot is not None and (
            learner_state_snapshot.course_id != course_id
            or learner_state_snapshot.class_id != class_id
            or learner_state_snapshot.learner_id != learner_id
        ):
            raise DomainError(
                code="LEARNER_STATE_MISMATCH",
                module="m4",
                message="learner state must match the task identity",
            )
