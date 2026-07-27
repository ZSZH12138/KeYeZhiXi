"""The deterministic, state-machine-constrained M6 policy action space."""

from __future__ import annotations

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    TutoringPolicyContext,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import DEFAULT_STATE_MACHINE


class SafetyEnvelope:
    """Return stable candidates without ever extending the public state graph."""

    action_space_version = "m6-action-space-v1"

    def candidates_for(
        self,
        context: TutoringPolicyContext,
    ) -> tuple[CandidateAction, ...]:
        """Build the complete ordered candidate set for this safe context."""

        signals = context.signals
        remediation = signals.needs_remediation()
        if context.current_state == "S0":
            next_states = ("S1",)
        elif context.current_state == "S1":
            next_states = ("S2",) if remediation else ("S3", "S2")
        elif context.current_state == "S2":
            next_states = ("S3",)
        elif context.current_state == "S3":
            next_states = ("S4",)
        elif context.current_state == "S4":
            next_states = self._practice_candidates(context)
        else:
            next_states = ()

        exploration_allowed = not remediation and context.current_state != "S5"
        candidates = tuple(
            _candidate(context.current_state, next_state, exploration_allowed)
            for next_state in next_states
        )
        for candidate in candidates:
            DEFAULT_STATE_MACHINE.validate_transition(
                context.current_state, candidate.next_state
            )
        return candidates

    @staticmethod
    def _practice_candidates(context: TutoringPolicyContext) -> tuple[str, ...]:
        signals = context.signals
        if signals.needs_remediation():
            return ("S2",)
        if (
            signals.has_diagnosed_misconception
            or not signals.has_new_evidence
            or signals.minimum_recent_correction_rate < 0.8
            or signals.minimum_mastery_confidence < 0.6
            or signals.maximum_hint_dependency > 0.0
        ):
            return ("S3", "S2")
        return ("S5", "S3", "S2")


def _candidate(
    current_state: str,
    next_state: str,
    exploration_allowed: bool,
) -> CandidateAction:
    action_type, prompt_template_id = _ACTION_DETAILS[next_state]
    return CandidateAction(
        candidate_id=f"m6.transition.{current_state.lower()}_to_{next_state.lower()}.v1",
        next_state=next_state,
        action_type=action_type,
        prompt_template_id=prompt_template_id,
        exploration_allowed=exploration_allowed,
    )


_ACTION_DETAILS: dict[str, tuple[str, str]] = {
    "S1": ("diagnostic_probe", "m6.s1.diagnostic_probe.v1"),
    "S2": ("minimal_hint", "m6.s2.minimal_hint.v1"),
    "S3": ("guided_question", "m6.s3.guided_question.v1"),
    "S4": ("self_explanation_prompt", "m6.s4.self_explanation.v1"),
    "S5": ("summary_and_transfer", "m6.s5.summary.v1"),
}
