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


def test_plain_observed_reward_preserves_its_raw_formula_components() -> None:
    """Basic observed outcomes remain independently auditable."""

    reward = reward_from_outcome(_outcome())

    assert reward.transfer_success == 0.8
    assert reward.additional_hint_count == 3
    assert reward.loop_count == 2
    assert reward.has_raw_outcome is True


def test_reward_record_preserves_every_evidenced_raw_outcome_component() -> None:
    """Catch reward persistence keeping only a scalar and losing audit evidence."""

    outcome = PolicyOutcome(
        policy_execution_fingerprint="e" * 64,
        status="observed",
        transfer_success=0.8,
        hint_count=3,
        loop_count=2,
        independent_correction_success=True,
        self_explanation_passed=False,
        additional_turn_count=4,
        teacher_review_escalated=True,
        safety_flag=False,
        outcome_event_ids=("event-1", "event-2"),
        outcome_watermark="watermark-7",
        observed_at="2026-07-27T13:00:00+00:00",
    )

    reward = reward_from_outcome(outcome)

    assert reward.reward == pytest.approx(0.45)
    assert reward.transfer_success == 0.8
    assert reward.independent_correction_success is True
    assert reward.self_explanation_passed is False
    assert reward.additional_hint_count == 3
    assert reward.additional_turn_count == 4
    assert reward.loop_count == 2
    assert reward.teacher_review_escalated is True
    assert reward.safety_flag is False
    assert reward.outcome_event_ids == ("event-1", "event-2")
    assert reward.outcome_watermark == "watermark-7"
    assert reward.observed_at == "2026-07-27T13:00:00+00:00"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("outcome_event_ids", (r"C:\Users\learner\events.json",)),
        ("outcome_watermark", "learner 42 completed"),
        ("outcome_event_ids", ("sk-proj-1234567890abcdefghijklmnop",)),
        ("outcome_watermark", "student@example.edu"),
    ],
)
@pytest.mark.parametrize("record_type", ("outcome", "reward"))
def test_reward_audit_provenance_rejects_path_or_free_text_identifiers(
    field_name: str,
    invalid_value: object,
    record_type: str,
) -> None:
    audit_fields = {field_name: invalid_value}
    if record_type == "outcome":
        with pytest.raises(ValueError, match="safe audit identifier"):
            PolicyOutcome(
                policy_execution_fingerprint="e" * 64,
                status="observed",
                transfer_success=0.8,
                hint_count=0,
                loop_count=0,
                observed_at="2026-07-27T13:00:00+00:00",
                **audit_fields,
            )
        return

    with pytest.raises(ValueError, match="safe audit identifier"):
        PolicyRewardRecord(
            policy_execution_fingerprint="e" * 64,
            outcome_identity="outcome_1",
            status="observed",
            reward=0.8,
            observed_at="2026-07-27T13:00:00+00:00",
            **audit_fields,
        )


def test_safety_flag_can_only_produce_an_invalid_non_reward_record() -> None:
    """Catch a safety violation entering OPE as an ordinary approved reward."""

    with pytest.raises(ValueError, match="safety_flag"):
        PolicyOutcome(
            policy_execution_fingerprint="e" * 64,
            status="observed",
            transfer_success=0.8,
            hint_count=0,
            loop_count=0,
            safety_flag=True,
            observed_at="2026-07-27T13:00:00+00:00",
        )

    invalid = reward_from_outcome(
        PolicyOutcome(
            policy_execution_fingerprint="e" * 64,
            status="invalid",
            transfer_success=0.8,
            hint_count=0,
            loop_count=0,
            safety_flag=True,
            observed_at="2026-07-27T13:00:00+00:00",
        )
    )

    assert invalid.status == "invalid"
    assert invalid.reward is None
    assert invalid.safety_flag is True


def test_rich_outcome_rejects_a_naive_observed_at_timestamp() -> None:
    """Catch audit timestamps that cannot be placed on an absolute timeline."""

    with pytest.raises(ValueError, match="timezone"):
        PolicyOutcome(
            policy_execution_fingerprint="e" * 64,
            status="observed",
            transfer_success=0.8,
            hint_count=0,
            loop_count=0,
            observed_at="2026-07-27T13:00:00",
        )


def test_legacy_reward_payload_keeps_its_original_checksum() -> None:
    """Catch raw outcome additions silently rewriting stored v10 rewards."""

    reward = PolicyRewardRecord(
        policy_execution_fingerprint="e" * 64,
        outcome_identity="o" * 64,
        status="observed",
        reward=0.45,
    )

    assert reward.canonical_json() == (
        '{"outcome_identity":"oooooooooooooooooooooooooooooooo'
        'oooooooooooooooooooooooooooooooo",'
        '"policy_execution_fingerprint":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
        'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","reward":0.45,'
        '"reward_version":"m6-reward-v1","status":"observed"}'
    )
    assert reward.identity == (
        "2c6e7b3287029768540bbe7a719cf983b9d1a5b6f1933f5146b42a13ebf2c77b"
    )


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
