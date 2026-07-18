"""Formal M6 tutoring-control service boundary."""

from __future__ import annotations

from typing import Any

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import StateUpdateResult
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.tutoring import (
    FeedbackGenerationTask,
    SessionStateSnapshot,
    TeachingAction,
    TutoringControlResult,
)
from course_insight.modules.m6_tutoring_fsm.repository import M6Repository
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
    DefaultTutoringStateMachine,
)


class M6TutoringControlService:
    """Select one safe teaching action through the authoritative FSM."""

    def __init__(
        self,
        state_machine_definition: Any,
        repository: M6Repository,
    ) -> None:
        self._state_machine_definition = state_machine_definition
        self._repository = repository

    def decide_next_action(
        self,
        task_plan: TaskPlan,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        previous_session_state_snapshot: SessionStateSnapshot | None,
    ) -> TutoringControlResult:
        """Select and package the next tutoring action.

        原始输入：M4 任务、M8 评分、M5 状态和可选 M6 会话历史。
        契约来源：四个直接前驱或本模块自历史输出。
        返回消费者：M2 证据检索、M7 反馈和下一轮 M4。
        业务校验：状态迁移、任务身份、查询引用和防泄题标志必须一致。
        错误码：INVALID_STATE_TRANSITION。
        """

        learner = state_update_result.learner_state_snapshot
        if (
            task_plan.learner_id != scoring_result_bundle.learner_id
            or learner.learner_id != task_plan.learner_id
            or learner.course_id != task_plan.course_id
            or learner.class_id != task_plan.class_id
        ):
            raise DomainError(
                code="TUTORING_REFERENCE_MISMATCH",
                module="m6",
                message="task, scoring, and state identities must align",
                details={"task_id": task_plan.task_id},
            )
        session = (
            previous_session_state_snapshot.model_copy(deep=True)
            if previous_session_state_snapshot is not None
            else SessionStateSnapshot(
                session_id=task_plan.session_id,
                current_state="S1",
                turn_count=0,
                completed_action_ids=[],
                updated_at=scoring_result_bundle.finalized_at,
            )
        )
        if session.session_id != task_plan.session_id:
            raise DomainError(
                code="TUTORING_REFERENCE_MISMATCH",
                module="m6",
                message="session history belongs to another task session",
            )
        diagnosis = state_update_result.diagnosis_result
        has_misconception = bool(diagnosis.priority_misconception_ids)
        machine = (
            self._state_machine_definition
            if isinstance(self._state_machine_definition, DefaultTutoringStateMachine)
            else DEFAULT_STATE_MACHINE
        )
        next_state = machine.choose_next(
            session.current_state,
            needs_review=scoring_result_bundle.requires_teacher_review(),
            has_misconception=has_misconception,
        )
        targets = diagnosis.priority_concept_ids or [
            learner.concept_states[0].concept_id
        ]
        turn = session.turn_count + 1
        action_id = f"{task_plan.task_id}_action_{turn}_{next_state}"
        action = TeachingAction(
            action_id=action_id,
            state_before=session.current_state,
            action_type=("minimal_hint" if next_state == "S2" else "guided_practice"),
            target_concept_ids=targets[:1],
            prompt_template_id=f"placeholder_{next_state.casefold()}_hint",
            must_not_reveal_answer=True,
            next_state=next_state,
            reason="Use governed diagnosis evidence for the smallest safe next step.",
        )
        query_id = f"{task_plan.task_id}_feedback_query_{turn}"
        query = EvidenceQuery(
            query_id=query_id,
            course_package_id=task_plan.course_package_id,
            query_text=f"Find the governed rule for concept {targets[0]}.",
            concept_ids=targets[:1],
            item_id=None,
            use_case="feedback",
            top_k=3,
            min_relevance=0.2,
        )
        feedback_task = FeedbackGenerationTask(
            feedback_task_id=f"{task_plan.task_id}_feedback_{turn}",
            task_id=task_plan.task_id,
            learner_id=task_plan.learner_id,
            teaching_action=action,
            diagnosis_result=diagnosis,
            score_summary={
                "score": scoring_result_bundle.total_score,
                "max_score": scoring_result_bundle.max_score,
            },
            learner_state_snapshot_id=learner.snapshot_id,
            evidence_query_id=query_id,
            created_at=scoring_result_bundle.finalized_at,
        )
        session.transition(next_state, action_id)
        session.updated_at = scoring_result_bundle.finalized_at
        return TutoringControlResult(
            teaching_action=action,
            feedback_generation_task=feedback_task,
            evidence_query=query,
            session_state_snapshot=session,
            created_at=scoring_result_bundle.finalized_at,
        )
