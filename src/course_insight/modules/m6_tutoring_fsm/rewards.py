"""Versioned, immutable reward association for M6 policy outcomes."""

from __future__ import annotations

import math
from typing import Protocol

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyOutcome,
    PolicyRewardRecord,
)


REWARD_VERSION = "m6-reward-v1"


class RewardRepository(Protocol):
    """The existing M6-private reward persistence capability."""

    def get_policy_reward(
        self,
        policy_execution_fingerprint: str,
        reward_version: str = REWARD_VERSION,
    ) -> PolicyRewardRecord | None:
        """Load one reward version for an execution."""

    def save_policy_reward(
        self,
        reward: PolicyRewardRecord,
    ) -> PolicyRewardRecord:
        """Insert or verify one immutable reward version."""


def reward_from_outcome(outcome: PolicyOutcome) -> PolicyRewardRecord:
    """Associate one outcome with v1 reward semantics without filling missing data."""

    if not isinstance(outcome, PolicyOutcome):
        raise TypeError("outcome must be a PolicyOutcome")
    value: float | None = None
    if outcome.status == "observed":
        assert outcome.transfer_success is not None
        try:
            value = (
                outcome.transfer_success
                - 0.05 * outcome.hint_count
                - 0.10 * outcome.loop_count
            )
        except OverflowError as error:
            raise ValueError("computed reward must be finite") from error
        if not math.isfinite(value):
            raise ValueError("computed reward must be finite")
    return PolicyRewardRecord(
        policy_execution_fingerprint=outcome.policy_execution_fingerprint,
        outcome_identity=outcome.identity,
        status=outcome.status,
        reward=value,
        reward_version=REWARD_VERSION,
    )


def persist_reward(
    repository: RewardRepository,
    outcome: PolicyOutcome,
) -> PolicyRewardRecord:
    """Idempotently persist one outcome-derived reward, rejecting rewrites."""

    candidate = reward_from_outcome(outcome)
    existing = repository.get_policy_reward(
        candidate.policy_execution_fingerprint,
        candidate.reward_version,
    )
    if existing is not None:
        if existing != candidate:
            raise ValueError("policy reward outcome conflict")
        return existing
    stored = repository.save_policy_reward(candidate)
    if stored != candidate:
        raise ValueError("policy reward outcome conflict")
    return stored
