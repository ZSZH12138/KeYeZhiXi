"""Safe deterministic M6 action and downstream task construction."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.state import StateUpdateResult
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    FeedbackGenerationTask,
    SessionStateSnapshot,
    TeachingAction,
    TutoringControlResult,
)
from course_insight.modules.m6_tutoring_fsm.decision_policy import DecisionSignals
from course_insight.modules.m6_tutoring_fsm.identity import derive_identifier


_ACTION_TEMPLATES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "S1": ("diagnostic_probe", "m6.s1.diagnostic_probe.v1"),
        "S2": ("minimal_hint", "m6.s2.minimal_hint.v1"),
        "S3": ("guided_question", "m6.s3.guided_question.v1"),
        "S4": ("self_explanation_prompt", "m6.s4.self_explanation.v1"),
        "S5": ("summary_and_transfer", "m6.s5.summary.v1"),
    }
)
_SAFE_QUERY_TEXT = (
    "Retrieve governed course rules, explanations, distinctions, and examples "
    "for the selected concepts."
)


def build_tutoring_result(
    *,
    task_plan: TaskPlan,
    scoring_result_bundle: ScoringResultBundle,
    state_update_result: StateUpdateResult,
    previous_session_state_snapshot: SessionStateSnapshot,
    next_state: str,
    target_concept_ids: list[str],
    authoritative_input_fingerprint: str,
    policy_version: str,
    signals: DecisionSignals,
) -> TutoringControlResult:
    """Build one aligned action, evidence query, feedback task, and new cursor."""

    action_type, template_id = _ACTION_TEMPLATES[next_state]
    action_id = derive_identifier("action", authoritative_input_fingerprint)
    query_id = derive_identifier("query", authoritative_input_fingerprint)
    feedback_task_id = derive_identifier(
        "feedback_task",
        authoritative_input_fingerprint,
    )
    targets = list(target_concept_ids)
    previous = previous_session_state_snapshot
    action = TeachingAction(
        action_id=action_id,
        state_before=previous.current_state,
        action_type=action_type,
        target_concept_ids=targets,
        prompt_template_id=template_id,
        must_not_reveal_answer=True,
        next_state=next_state,
        reason=(
            f"Policy {policy_version} selected {previous.current_state}->{next_state} "
            f"from {_reason_basis(previous.current_state, next_state, signals)} signals."
        ),
    )
    query = EvidenceQuery(
        query_id=query_id,
        course_package_id=task_plan.course_package_id,
        query_text=_SAFE_QUERY_TEXT,
        concept_ids=targets,
        item_id=None,
        use_case="feedback",
        top_k=3,
        min_relevance=0.2,
    )
    feedback_task = FeedbackGenerationTask(
        feedback_task_id=feedback_task_id,
        task_id=task_plan.task_id,
        learner_id=task_plan.learner_id,
        teaching_action=action,
        diagnosis_result=state_update_result.diagnosis_result.model_copy(deep=True),
        score_summary={
            "score": scoring_result_bundle.total_score,
            "max_score": scoring_result_bundle.max_score,
        },
        learner_state_snapshot_id=(
            state_update_result.learner_state_snapshot.snapshot_id
        ),
        evidence_query_id=query_id,
        created_at=scoring_result_bundle.finalized_at,
    )
    session = SessionStateSnapshot(
        session_id=previous.session_id,
        current_state=next_state,
        turn_count=previous.turn_count + 1,
        completed_action_ids=[*previous.completed_action_ids, action_id],
        updated_at=scoring_result_bundle.finalized_at,
    )
    return TutoringControlResult(
        teaching_action=action,
        feedback_generation_task=feedback_task,
        evidence_query=query,
        session_state_snapshot=session,
        created_at=scoring_result_bundle.finalized_at,
    )


def _reason_basis(
    current_state: str,
    next_state: str,
    signals: DecisionSignals,
) -> str:
    if next_state == "S2":
        return "review_or_active_remediation"
    if next_state == "S5":
        return "new_evidence_and_stable_mastery"
    if current_state == "S4" and (
        signals.has_diagnosed_misconception or not signals.has_new_evidence
    ):
        return "evidence_consolidation"
    return "governed_progression"
