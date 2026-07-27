"""Reward lifecycle tests for private M6 policy learning."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyOutcome,
    PolicyRewardRecord,
)
from course_insight.modules.m6_tutoring_fsm.rewards import (
    REWARD_VERSION,
    persist_reward,
    reward_from_outcome,
)


@dataclass
class _RewardRepository:
    records: dict[tuple[str, str], PolicyRewardRecord] = field(
        default_factory=dict
    )

    def get_policy_reward(
        self,
        policy_execution_fingerprint: str,
        reward_version: str = "m6-reward-v1",
    ) -> PolicyRewardRecord | None:
        return self.records.get(
            (policy_execution_fingerprint, reward_version)
        )

    def save_policy_reward(
        self,
        reward: PolicyRewardRecord,
    ) -> PolicyRewardRecord:
        key = (
            reward.policy_execution_fingerprint,
            reward.reward_version,
        )
        existing = self.records.get(key)
        if existing is not None and existing != reward:
            raise ValueError("policy reward identity conflict")
        self.records = {**self.records, key: reward}
        return reward


def _outcome(
    *,
    status: str = "observed",
    transfer_success: float | None = 0.8,
    hint_count: int = 3,
    loop_count: int = 2,
) -> PolicyOutcome:
    return PolicyOutcome(
        policy_execution_fingerprint="e" * 64,
        status=status,
        transfer_success=transfer_success,
        hint_count=hint_count,
        loop_count=loop_count,
    )


def test_reward_v1_uses_the_hand_checked_transfer_hint_and_loop_formula() -> None:
    """Catch a wrong coefficient, sign, or reward version."""

    reward = reward_from_outcome(_outcome())

    assert REWARD_VERSION == "m6-reward-v1"
    assert reward.reward_version == "m6-reward-v1"
    assert reward.status == "observed"
    assert reward.reward == pytest.approx(0.45)


@pytest.mark.parametrize("status", ("pending", "censored"))
def test_missing_follow_up_remains_missing_instead_of_becoming_zero(
    status: str,
) -> None:
    """Catch missing follow-up evidence being fabricated as reward zero."""

    outcome = _outcome(
        status=status,
        transfer_success=None,
        hint_count=1,
        loop_count=1,
    )

    reward = reward_from_outcome(outcome)

    assert reward.status == status
    assert reward.reward is None
    assert reward.outcome_identity == outcome.identity


@pytest.mark.parametrize(
    "values",
    (
        {
            "status": "observed",
            "transfer_success": None,
            "hint_count": 0,
            "loop_count": 0,
        },
        {
            "status": "pending",
            "transfer_success": 0.0,
            "hint_count": 0,
            "loop_count": 0,
        },
        {
            "status": "observed",
            "transfer_success": 1.0,
            "hint_count": -1,
            "loop_count": 0,
        },
    ),
)
def test_invalid_outcomes_are_rejected_before_reward_association(
    values: dict[str, object],
) -> None:
    """Catch invalid or internally contradictory outcome evidence."""

    with pytest.raises(ValueError):
        PolicyOutcome(
            policy_execution_fingerprint="e" * 64,
            **values,  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match="PolicyOutcome"):
        reward_from_outcome(values)  # type: ignore[arg-type]


def test_reward_persistence_is_idempotent_and_never_overwrites_an_outcome() -> None:
    """Catch replay creating duplicates or replacing immutable evidence."""

    repository = _RewardRepository()
    outcome = _outcome()

    first = persist_reward(repository, outcome)
    replay = persist_reward(repository, outcome)

    assert replay is first
    assert len(repository.records) == 1

    conflicting = _outcome(transfer_success=0.4)
    with pytest.raises(ValueError, match="conflict"):
        persist_reward(repository, conflicting)
    assert tuple(repository.records.values()) == (first,)
