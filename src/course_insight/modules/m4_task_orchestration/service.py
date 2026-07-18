"""Formal M4 task-orchestration service boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.state import LearnerStateSnapshot
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m4_task_orchestration.repository import M4Repository


_FIXED_TIME = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))
_ASSESSMENT_TASK_TYPES = frozenset(
    {"diagnostic", "practice", "correction", "stage_assessment"}
)
_SUPPORTED_TASK_TYPES = frozenset({"qa", *_ASSESSMENT_TASK_TYPES})
_ASSESSMENT_WORKFLOW = ["M8", "M2", "M7", "M5", "M6", "M9"]


class M4TaskOrchestrationService:
    """Convert learner intent into an idempotent module workflow."""

    def __init__(
        self,
        repository: M4Repository,
        idempotency_key_factory: Any,
    ) -> None:
        self._repository = repository
        self._idempotency_key_factory = idempotency_key_factory

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

        task_type = self._resolve_task_type(student_text, task_type_hint)
        self._validate_inputs(
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            knowledge_bundle=knowledge_bundle,
            learner_state_snapshot=learner_state_snapshot,
        )
        blueprint_id = self._resolve_blueprint_id(
            task_type=task_type,
            course_id=course_id,
            knowledge_bundle=knowledge_bundle,
        )
        identity_parts = [
            course_id,
            class_id,
            learner_id,
            session_id,
            task_type,
            knowledge_bundle.knowledge_bundle_id,
            knowledge_bundle.course_package_id,
            blueprint_id or "",
        ]
        canonical_identity = json.dumps(
            identity_parts,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        idempotency_key = hashlib.sha256(
            canonical_identity.encode("utf-8")
        ).hexdigest()
        task_id = f"task_{idempotency_key}"

        getter = getattr(self._repository, "get_task_plan", None)
        if callable(getter):
            existing = getter(task_id)
            if existing is not None:
                return existing.model_copy(deep=True)

        workflow = (
            list(_ASSESSMENT_WORKFLOW)
            if task_type in _ASSESSMENT_TASK_TYPES
            else ["M2", "M7", "M6"]
        )
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
            created_at=_FIXED_TIME,
        )
        saver = getattr(self._repository, "save_task_plan", None)
        if callable(saver):
            saver(plan.model_copy(deep=True), idempotency_key)
        return plan

    @staticmethod
    def _resolve_task_type(student_text: str, task_type_hint: str | None) -> str:
        if not student_text.strip():
            raise DomainError(
                code="UNSUPPORTED_TASK",
                module="m4",
                message="student task text must not be blank",
                recoverable=True,
            )
        if task_type_hint is None:
            normalized_text = " ".join(student_text.split()).casefold()
            task_type = (
                "stage_assessment"
                if "assessment" in normalized_text or "测评" in normalized_text
                else "qa"
            )
        else:
            task_type = " ".join(task_type_hint.split()).casefold()
        if task_type not in _SUPPORTED_TASK_TYPES:
            raise DomainError(
                code="UNSUPPORTED_TASK",
                module="m4",
                message="task type is not supported",
                details={"task_type": task_type},
                recoverable=True,
            )
        return task_type

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

    @staticmethod
    def _resolve_blueprint_id(
        *,
        task_type: str,
        course_id: str,
        knowledge_bundle: KnowledgeBundle,
    ) -> str | None:
        if task_type not in _ASSESSMENT_TASK_TYPES:
            return None
        for blueprint in knowledge_bundle.blueprints:
            if (
                blueprint.course_id == course_id
                and " ".join(blueprint.status.split()).casefold()
                == "teacher_approved"
            ):
                return blueprint.blueprint_id
        raise DomainError(
            code="BLUEPRINT_NOT_FOUND",
            module="m4",
            message="assessment task requires a teacher-approved blueprint",
            details={"course_id": course_id},
            recoverable=True,
        )
