"""Adapters which rank only candidates supplied by the M6 safety envelope."""

from __future__ import annotations

from typing import Protocol, Sequence

from course_insight.modules.m6_tutoring_fsm.decision_policy import M6DecisionPolicy
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    PolicyDecision,
    TutoringPolicyContext,
)


class PolicyAdapter(Protocol):
    """Private protocol for adapters which may select, but never create, actions."""

    adapter_id: str
    adapter_version: str

    def select(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyDecision:
        """Return one member of ``candidates`` for this context."""


class RulesPolicyAdapter:
    """Expose the existing deterministic policy through the candidate interface."""

    adapter_id = "m6-rules-adapter"
    adapter_version = "v1"

    def __init__(self, policy: M6DecisionPolicy | None = None) -> None:
        self._policy = policy if policy is not None else M6DecisionPolicy()

    @property
    def policy_id(self) -> str:
        return self._policy.policy_version

    def select(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyDecision:
        """Choose the baseline next state, requiring it to be safety supplied."""

        ordered = tuple(candidates)
        if not ordered:
            raise ValueError("rules adapter requires at least one candidate")
        baseline_state = self._policy.decide_next_state(
            context.current_state, context.signals
        )
        selected = next(
            (candidate for candidate in ordered if candidate.next_state == baseline_state),
            None,
        )
        if selected is None:
            raise ValueError("baseline action is not present in safety candidates")
        return PolicyDecision(
            request_fingerprint=context.request_fingerprint,
            mode="rules",
            selected_candidate_id=selected.candidate_id,
            candidate_ids=tuple(candidate.candidate_id for candidate in ordered),
            prediction=None,
        )
