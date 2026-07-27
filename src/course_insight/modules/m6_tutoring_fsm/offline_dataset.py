"""Canonical de-identified datasets for M6 offline policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import math
import re
from typing import Any, Mapping

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyObservation,
    PolicyRewardRecord,
    TUTORING_STATES,
)


EXPORT_FIELDS: tuple[str, ...] = (
    "decision_id",
    "context",
    "candidate_actions",
    "chosen_action",
    "propensity",
    "reward",
    "reward_status",
    "policy_version",
    "feature_schema_version",
    "action_space_version",
    "anonymous_group_key",
    "occurred_at",
    "session_id",
    "event_time",
    "state",
    "target_propensities",
    "direct_estimates",
)

_STRUCTURED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class OfflinePolicyRow:
    """One observed reward with only structured, de-identified policy fields."""

    group_id: str
    session_id: str
    event_time: int
    state: str
    selected_action: str
    candidate_actions: tuple[str, ...]
    logging_propensity: float | None
    target_propensities: object
    reward: float | None
    direct_estimates: object
    decision_id: str
    context: object
    reward_status: str
    policy_version: str
    feature_schema_version: str
    action_space_version: str
    occurred_at: str

    def __post_init__(self) -> None:
        _require_sha256(self.group_id, "group_id")
        _require_sha256(self.session_id, "session_id")
        if type(self.event_time) is not int or self.event_time < 0:
            raise ValueError("event_time must be a non-negative integer")
        if self.state not in TUTORING_STATES:
            raise ValueError("state must be a tutoring state")
        candidates = _normalize_candidates(self.candidate_actions)
        object.__setattr__(self, "candidate_actions", candidates)
        _require_structured_id(self.selected_action, "selected_action")
        if self.selected_action not in candidates:
            raise ValueError("selected_action must be a candidate")
        if self.logging_propensity is not None:
            _require_probability(
                self.logging_propensity,
                "logging_propensity",
            )
            object.__setattr__(
                self,
                "logging_propensity",
                float(self.logging_propensity),
            )
        target = _normalize_action_values(
            self.target_propensities,
            candidates,
            "target_propensities",
            probability=True,
        )
        if not math.isclose(
            sum(value for _, value in target),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("target_propensities must sum to one")
        object.__setattr__(self, "target_propensities", target)
        if self.reward_status not in {
            "pending",
            "observed",
            "censored",
            "invalid",
        }:
            raise ValueError("reward_status is not supported")
        if self.reward_status == "observed":
            if self.reward is None:
                raise ValueError("observed offline row requires reward")
            _require_finite(self.reward, "reward")
            object.__setattr__(self, "reward", float(self.reward))
        elif self.reward is not None:
            raise ValueError("non-observed offline row must not set reward")
        object.__setattr__(
            self,
            "direct_estimates",
            _normalize_action_values(
                self.direct_estimates,
                candidates,
                "direct_estimates",
                probability=False,
            ),
        )
        _require_sha256(self.decision_id, "decision_id")
        object.__setattr__(self, "context", _normalize_context(self.context))
        for name in (
            "policy_version",
            "feature_schema_version",
            "action_space_version",
        ):
            _require_structured_id(getattr(self, name), name)
        _require_timezone(self.occurred_at, "occurred_at")

    def canonical_payload(self) -> dict[str, Any]:
        """Return the explicit export allowlist in canonical field form."""

        return {
            "decision_id": self.decision_id,
            "context": dict(self.context),
            "candidate_actions": list(self.candidate_actions),
            "chosen_action": self.selected_action,
            "propensity": self.logging_propensity,
            "reward": self.reward,
            "reward_status": self.reward_status,
            "policy_version": self.policy_version,
            "feature_schema_version": self.feature_schema_version,
            "action_space_version": self.action_space_version,
            "anonymous_group_key": self.group_id,
            "occurred_at": self.occurred_at,
            "session_id": self.session_id,
            "event_time": self.event_time,
            "state": self.state,
            "target_propensities": dict(self.target_propensities),
            "direct_estimates": dict(self.direct_estimates),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def identity(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OfflineDatasetSplit:
    """A group-isolated chronological train/evaluation partition."""

    train: tuple[OfflinePolicyRow, ...]
    evaluation: tuple[OfflinePolicyRow, ...]

    def __post_init__(self) -> None:
        if not self.train or not self.evaluation:
            raise ValueError("both dataset split partitions must be non-empty")
        train_groups = {row.group_id for row in self.train}
        evaluation_groups = {row.group_id for row in self.evaluation}
        if train_groups & evaluation_groups:
            raise ValueError("dataset groups must not cross split partitions")
        train_sessions = {row.session_id for row in self.train}
        evaluation_sessions = {row.session_id for row in self.evaluation}
        if train_sessions & evaluation_sessions:
            raise ValueError("dataset sessions must not cross split partitions")
        if max(row.event_time for row in self.train) >= min(
            row.event_time for row in self.evaluation
        ):
            raise ValueError(
                "dataset split requires a strict whole-group time boundary"
            )


def build_offline_row(
    observation: PolicyObservation,
    reward: PolicyRewardRecord,
    *,
    deidentification_key: bytes,
    raw_group_id: str,
    raw_session_id: str,
    event_time: int,
    state: str,
    target_propensities: object,
    direct_estimates: object,
) -> OfflinePolicyRow:
    """Join immutable M6 records while hashing all raw grouping identities."""

    if not isinstance(observation, PolicyObservation):
        raise TypeError("observation must be a PolicyObservation")
    if not isinstance(reward, PolicyRewardRecord):
        raise TypeError("reward must be a PolicyRewardRecord")
    if (
        reward.policy_execution_fingerprint
        != observation.policy_execution_fingerprint
    ):
        raise ValueError("reward and observation execution identities differ")
    if reward.status != "observed" or reward.reward is None:
        raise ValueError("offline rows require an observed reward")
    _require_deidentification_key(deidentification_key)
    group_id = _pseudonymize(
        "group",
        raw_group_id,
        deidentification_key,
    )
    return OfflinePolicyRow(
        group_id=group_id,
        session_id=_pseudonymize(
            "session",
            raw_session_id,
            deidentification_key,
        ),
        event_time=event_time,
        state=state,
        selected_action=observation.selected_candidate_id,
        candidate_actions=observation.candidate_ids,
        logging_propensity=observation.propensity,
        target_propensities=target_propensities,
        reward=reward.reward,
        direct_estimates=direct_estimates,
        decision_id=_pseudonymize(
            "decision",
            (
                observation.decision_id
                if observation.decision_id is not None
                else observation.request_fingerprint
            ),
            deidentification_key,
        ),
        context={
            "context_checksum": (
                observation.context_checksum
                if observation.context_checksum is not None
                else sha256(
                    observation.canonical_json().encode("utf-8")
                ).hexdigest()
            ),
            "state": state,
        },
        reward_status=reward.status,
        policy_version=(
            observation.logging_policy_id
            if observation.logging_policy_id is not None
            else "legacy-unknown"
        ),
        feature_schema_version=observation.feature_schema_version,
        action_space_version=(
            observation.action_space_version
            if observation.action_space_version is not None
            else "legacy-unknown"
        ),
        occurred_at=(
            observation.created_at
            if observation.created_at is not None
            else datetime.fromtimestamp(event_time, timezone.utc).isoformat()
        ),
    )


def canonical_jsonl(rows: tuple[OfflinePolicyRow, ...]) -> str:
    """Serialize rows deterministically, independent of caller ordering."""

    ordered = _canonical_rows(rows)
    return "\n".join(row.canonical_json() for row in ordered)


def dataset_identity(rows: tuple[OfflinePolicyRow, ...]) -> str:
    """Return the SHA-256 identity of the exact canonical JSONL bytes."""

    return sha256(canonical_jsonl(rows).encode("utf-8")).hexdigest()


def grouped_time_split(
    rows: tuple[OfflinePolicyRow, ...],
    *,
    evaluation_fraction: float,
) -> OfflineDatasetSplit:
    """Assign newest whole groups to evaluation without session leakage."""

    ordered = _canonical_rows(rows)
    _require_finite(evaluation_fraction, "evaluation_fraction")
    fraction = float(evaluation_fraction)
    if not 0.0 < fraction < 1.0:
        raise ValueError("evaluation_fraction must be between zero and one")
    sessions: dict[str, str] = {}
    groups: dict[str, list[OfflinePolicyRow]] = {}
    for row in ordered:
        prior_group = sessions.get(row.session_id)
        if prior_group is not None and prior_group != row.group_id:
            raise ValueError("one session cannot belong to multiple groups")
        sessions = {**sessions, row.session_id: row.group_id}
        groups = {
            **groups,
            row.group_id: [*groups.get(row.group_id, []), row],
        }
    if len(groups) < 2:
        raise ValueError("grouped split requires at least two groups")
    chronological_groups = tuple(sorted(
        groups,
        key=lambda group_id: (
            min(row.event_time for row in groups[group_id]),
            max(row.event_time for row in groups[group_id]),
            group_id,
        ),
    ))
    desired_evaluation_group_count = min(
        len(chronological_groups) - 1,
        max(1, math.ceil(len(chronological_groups) * fraction)),
    )
    valid_boundaries = tuple(
        boundary
        for boundary in range(1, len(chronological_groups))
        if max(
            row.event_time
            for group_id in chronological_groups[:boundary]
            for row in groups[group_id]
        )
        < min(
            row.event_time
            for group_id in chronological_groups[boundary:]
            for row in groups[group_id]
        )
    )
    if not valid_boundaries:
        raise ValueError(
            "grouped split has no strict whole-group time boundary"
        )
    boundary = min(
        valid_boundaries,
        key=lambda candidate: (
            abs(
                (len(chronological_groups) - candidate)
                - desired_evaluation_group_count
            ),
            -(len(chronological_groups) - candidate),
        ),
    )
    evaluation_group_ids = set(
        chronological_groups[boundary:]
    )
    train = tuple(
        row for row in ordered if row.group_id not in evaluation_group_ids
    )
    evaluation = tuple(
        row for row in ordered if row.group_id in evaluation_group_ids
    )
    return OfflineDatasetSplit(train=train, evaluation=evaluation)


def _canonical_rows(
    rows: tuple[OfflinePolicyRow, ...],
) -> tuple[OfflinePolicyRow, ...]:
    if type(rows) not in {tuple, list} or not rows:
        raise ValueError("offline dataset must contain rows")
    if any(not isinstance(row, OfflinePolicyRow) for row in rows):
        raise TypeError("offline dataset must contain OfflinePolicyRow values")
    ordered = tuple(sorted(rows, key=lambda row: (row.event_time, row.identity)))
    if len({row.identity for row in ordered}) != len(ordered):
        raise ValueError("offline dataset must not contain duplicate rows")
    return ordered


def _pseudonymize(
    namespace: str,
    value: object,
    key: bytes,
) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"raw_{namespace}_id must be a non-blank string")
    payload = f"m6-offline-{namespace}-v1\0{value}".encode("utf-8")
    return hmac.digest(key, payload, "sha256").hex()


def _require_deidentification_key(value: object) -> None:
    if type(value) is not bytes or len(value) < 32:
        raise ValueError(
            "deidentification_key must contain at least 32 bytes"
        )


def _normalize_candidates(values: object) -> tuple[str, ...]:
    if type(values) not in {tuple, list} or not values:
        raise ValueError("candidate_actions must be a non-empty sequence")
    candidates = tuple(values)
    for candidate in candidates:
        _require_structured_id(candidate, "candidate_actions")
    if len(candidates) != len(set(candidates)):
        raise ValueError("candidate_actions must not contain duplicates")
    return candidates


def _normalize_action_values(
    values: object,
    candidates: tuple[str, ...],
    field_name: str,
    *,
    probability: bool,
) -> tuple[tuple[str, float], ...]:
    if isinstance(values, Mapping):
        entries = tuple(values.items())
    elif type(values) in {tuple, list}:
        entries = tuple(values)
    else:
        raise ValueError(f"{field_name} must contain action values")
    normalized: list[tuple[str, float]] = []
    for entry in entries:
        if type(entry) not in {tuple, list} or len(entry) != 2:
            raise ValueError(f"{field_name} must contain action values")
        action, value = entry
        _require_structured_id(action, field_name)
        if probability:
            _require_probability(value, field_name)
        else:
            _require_finite(value, field_name)
        normalized.append((str(action), float(value)))
    if len({action for action, _ in normalized}) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate actions")
    if {action for action, _ in normalized} != set(candidates):
        raise ValueError(f"{field_name} must cover exactly the candidates")
    return tuple(sorted(normalized))


def _normalize_context(values: object) -> tuple[tuple[str, str], ...]:
    if isinstance(values, Mapping):
        normalized = dict(values)
    elif type(values) in {tuple, list}:
        try:
            normalized = dict(values)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "context must contain context_checksum and state only"
            ) from error
    else:
        normalized = {}
    if set(normalized) != {
        "context_checksum",
        "state",
    }:
        raise ValueError(
            "context must contain context_checksum and state only"
        )
    checksum = normalized["context_checksum"]
    state = normalized["state"]
    _require_sha256(checksum, "context_checksum")
    if state not in TUTORING_STATES:
        raise ValueError("context state must be a tutoring state")
    return (("context_checksum", str(checksum)), ("state", str(state)))


def _require_structured_id(value: object, field_name: str) -> None:
    if type(value) is not str or _STRUCTURED_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must contain structured identifiers")


def _require_sha256(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_finite(value: object, field_name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


def _require_probability(value: object, field_name: str) -> None:
    _require_finite(value, field_name)
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field_name} must be a probability")


def _require_timezone(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a timezone timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be a timezone timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone timestamp")
