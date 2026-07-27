"""Pure-Python deterministic off-policy evaluation for M6."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Protocol

from course_insight.modules.m6_tutoring_fsm.offline_dataset import (
    OfflinePolicyRow,
    dataset_identity,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyEvaluationRecord,
)


_METRICS: tuple[str, ...] = ("ips", "snips", "dm", "dr")


@dataclass(frozen=True, slots=True)
class OPEConfig:
    """Fail-closed sample, support, coverage, and approval thresholds."""

    minimum_rows: int = 20
    minimum_effective_sample_size: float = 10.0
    minimum_action_coverage: float = 0.8
    minimum_support_coverage: float = 0.95
    minimum_logging_propensity: float = 0.01
    approval_minimum_dr: float = 0.0
    bootstrap_samples: int = 200
    confidence_level: float = 0.95

    def __post_init__(self) -> None:
        if type(self.minimum_rows) is not int or self.minimum_rows <= 0:
            raise ValueError("minimum_rows must be a positive integer")
        _require_nonnegative_finite(
            self.minimum_effective_sample_size,
            "minimum_effective_sample_size",
        )
        for name in (
            "minimum_action_coverage",
            "minimum_support_coverage",
            "minimum_logging_propensity",
            "confidence_level",
        ):
            _require_probability(getattr(self, name), name)
        if not 0.0 < float(self.confidence_level) < 1.0:
            raise ValueError("confidence_level must be between zero and one")
        _require_finite(self.approval_minimum_dr, "approval_minimum_dr")
        if type(self.bootstrap_samples) is not int or self.bootstrap_samples <= 0:
            raise ValueError("bootstrap_samples must be a positive integer")


class EvaluationRepository(Protocol):
    """The existing M6-private evaluation persistence capability."""

    def get_policy_evaluation(
        self,
        policy_id: str,
        dataset_identity: str,
    ) -> PolicyEvaluationRecord | None:
        """Load one policy/dataset evaluation."""

    def save_policy_evaluation(
        self,
        evaluation: PolicyEvaluationRecord,
    ) -> PolicyEvaluationRecord:
        """Insert or verify one immutable evaluation."""


def evaluate_offline_policy(
    policy_id: str,
    rows: tuple[OfflinePolicyRow, ...],
    *,
    config: OPEConfig | None = None,
) -> PolicyEvaluationRecord:
    """Evaluate one target policy and return its complete persistable record."""

    if type(policy_id) is not str or not policy_id.strip():
        raise ValueError("policy_id must be a non-blank string")
    settings = OPEConfig() if config is None else config
    if not isinstance(settings, OPEConfig):
        raise TypeError("config must be an OPEConfig")
    ordered = _ordered_rows(rows)
    identity = dataset_identity(ordered)
    valid_rows = tuple(
        row
        for row in ordered
        if row.logging_propensity is not None
        and row.logging_propensity > 0.0
    )
    action_coverage, support_coverage = _state_action_coverage(
        ordered,
        settings.minimum_logging_propensity,
    )
    reasons: list[str] = []
    if len(ordered) < settings.minimum_rows:
        reasons.append("insufficient_rows")
    if len(valid_rows) != len(ordered):
        reasons.append("invalid_propensity")
    if (
        support_coverage < 1.0
        or support_coverage < settings.minimum_support_coverage
        or any(
            _target_probability(row, row.selected_action) > 0.0
            and row.logging_propensity is not None
            and 0.0
            < row.logging_propensity
            < settings.minimum_logging_propensity
            for row in ordered
        )
    ):
        reasons.append("low_support")
    if (
        action_coverage < 1.0
        or action_coverage < settings.minimum_action_coverage
    ):
        reasons.append("poor_action_coverage")

    metrics: dict[str, float] = {}
    confidence_intervals: dict[str, tuple[float, float]] = {}
    state_slices: tuple[dict[str, object], ...] = ()
    group_slices: tuple[dict[str, object], ...] = ()
    effective_sample_size = 0.0
    if len(valid_rows) == len(ordered):
        metrics, weights = _estimate(ordered)
        effective_sample_size = _effective_sample_size(weights)
        if (
            effective_sample_size
            < settings.minimum_effective_sample_size
        ):
            reasons.append("low_effective_sample_size")
        confidence_intervals = _bootstrap_intervals(
            ordered,
            identity,
            settings,
        )
        state_slices = _slice_summaries(
            ordered,
            lambda row: row.state,
        )
        group_slices = _slice_summaries(
            ordered,
            lambda row: row.group_id,
        )

    blocking_reasons = tuple(sorted(set(reasons)))
    status = (
        "insufficient_data"
        if blocking_reasons
        else "sufficient_data"
    )
    approved = False
    safety_reasons = list(blocking_reasons)
    if status == "sufficient_data":
        dr_lower = confidence_intervals["dr"][0]
        approved = dr_lower >= settings.approval_minimum_dr
        if not approved:
            safety_reasons.append("reward_below_approval_threshold")
    return PolicyEvaluationRecord(
        policy_id=policy_id,
        dataset_identity=identity,
        status=status,
        approved=approved,
        effective_sample_size=effective_sample_size,
        action_coverage=action_coverage,
        observation_count=len(ordered),
        metrics=metrics,
        confidence_intervals=confidence_intervals,
        state_slices=state_slices,
        group_slices=group_slices,
        support_coverage=support_coverage,
        safety_reasons=tuple(safety_reasons),
    )


def persist_evaluation(
    repository: EvaluationRepository,
    evaluation: PolicyEvaluationRecord,
) -> PolicyEvaluationRecord:
    """Idempotently save one complete evaluation without overwriting a verdict."""

    if not isinstance(evaluation, PolicyEvaluationRecord):
        raise TypeError("evaluation must be a PolicyEvaluationRecord")
    existing = repository.get_policy_evaluation(
        evaluation.policy_id,
        evaluation.dataset_identity,
    )
    if existing is not None:
        if existing == evaluation:
            return existing
        if (
            _is_legacy_evaluation_summary(existing)
            and _legacy_summary_fields(existing)
            == _legacy_summary_fields(evaluation)
        ):
            return existing
        raise ValueError("policy evaluation identity conflict")
    stored = repository.save_policy_evaluation(evaluation)
    if stored != evaluation:
        raise ValueError("policy evaluation identity conflict")
    return stored


def _ordered_rows(
    rows: tuple[OfflinePolicyRow, ...],
) -> tuple[OfflinePolicyRow, ...]:
    if type(rows) not in {tuple, list} or not rows:
        raise ValueError("offline evaluation requires rows")
    if any(not isinstance(row, OfflinePolicyRow) for row in rows):
        raise TypeError("offline evaluation requires OfflinePolicyRow values")
    ordered = tuple(sorted(rows, key=lambda row: (row.event_time, row.identity)))
    if len({row.identity for row in ordered}) != len(ordered):
        raise ValueError("offline evaluation rows must be unique")
    return ordered


def _estimate(
    rows: tuple[OfflinePolicyRow, ...],
) -> tuple[dict[str, float], tuple[float, ...]]:
    weights: list[float] = []
    ips_terms: list[float] = []
    dm_terms: list[float] = []
    dr_terms: list[float] = []
    for row in rows:
        assert row.logging_propensity is not None
        target_probability = _target_probability(
            row,
            row.selected_action,
        )
        weight = target_probability / row.logging_propensity
        direct = dict(row.direct_estimates)
        target = dict(row.target_propensities)
        dm = sum(
            target[action] * direct[action]
            for action in row.candidate_actions
        )
        logged_direct = direct[row.selected_action]
        weights.append(weight)
        ips_terms.append(weight * row.reward)
        dm_terms.append(dm)
        dr_terms.append(dm + weight * (row.reward - logged_direct))
    count = len(rows)
    total_weight = sum(weights)
    metrics = {
        "ips": sum(ips_terms) / count,
        "snips": (
            sum(ips_terms) / total_weight
            if total_weight > 0.0
            else 0.0
        ),
        "dm": sum(dm_terms) / count,
        "dr": sum(dr_terms) / count,
    }
    return metrics, tuple(weights)


def _effective_sample_size(weights: tuple[float, ...]) -> float:
    squared_sum = sum(weight * weight for weight in weights)
    if squared_sum == 0.0:
        return 0.0
    return sum(weights) ** 2 / squared_sum


def _state_action_coverage(
    rows: tuple[OfflinePolicyRow, ...],
    minimum_logging_propensity: float,
) -> tuple[float, float]:
    required_pairs = {
        (row.state, action)
        for row in rows
        for action, probability in row.target_propensities
        if probability > 0.0
    }
    observed_pairs = {
        (row.state, row.selected_action)
        for row in rows
        if row.logging_propensity is not None
        and row.logging_propensity > 0.0
    }
    supported_pairs = {
        (row.state, row.selected_action)
        for row in rows
        if row.logging_propensity is not None
        and row.logging_propensity > 0.0
        and row.logging_propensity >= minimum_logging_propensity
    }
    return (
        len(required_pairs & observed_pairs) / len(required_pairs),
        len(required_pairs & supported_pairs) / len(required_pairs),
    )


def _target_probability(row: OfflinePolicyRow, action: str) -> float:
    return dict(row.target_propensities)[action]


def _bootstrap_intervals(
    rows: tuple[OfflinePolicyRow, ...],
    identity: str,
    config: OPEConfig,
) -> dict[str, tuple[float, float]]:
    generator = random.Random(int(identity, 16))
    distributions: dict[str, list[float]] = {
        metric: [] for metric in _METRICS
    }
    for _ in range(config.bootstrap_samples):
        sample = tuple(
            rows[generator.randrange(len(rows))]
            for _ in range(len(rows))
        )
        metrics, _ = _estimate(sample)
        for metric in _METRICS:
            distributions[metric].append(metrics[metric])
    tail = (1.0 - config.confidence_level) / 2.0
    lower_index = math.floor(
        tail * (config.bootstrap_samples - 1)
    )
    upper_index = math.ceil(
        (1.0 - tail) * (config.bootstrap_samples - 1)
    )
    return {
        metric: (
            sorted(values)[lower_index],
            sorted(values)[upper_index],
        )
        for metric, values in distributions.items()
    }


def _slice_summaries(
    rows: tuple[OfflinePolicyRow, ...],
    key_for: object,
) -> tuple[dict[str, object], ...]:
    grouped: dict[str, list[OfflinePolicyRow]] = {}
    for row in rows:
        key = key_for(row)  # type: ignore[operator]
        grouped = {**grouped, key: [*grouped.get(key, []), row]}
    return tuple(
        {
            "key": key,
            "sample_size": len(grouped[key]),
            "metrics": _estimate(tuple(grouped[key]))[0],
        }
        for key in sorted(grouped)
    )


def _require_finite(value: object, field_name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


def _require_nonnegative_finite(value: object, field_name: str) -> None:
    _require_finite(value, field_name)
    if float(value) < 0.0:
        raise ValueError(f"{field_name} must be non-negative")


def _require_probability(value: object, field_name: str) -> None:
    _require_finite(value, field_name)
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field_name} must be a probability")


def _is_legacy_evaluation_summary(
    evaluation: PolicyEvaluationRecord,
) -> bool:
    return (
        evaluation.metrics == ()
        and evaluation.confidence_intervals == ()
        and evaluation.state_slices == ()
        and evaluation.group_slices == ()
        and evaluation.support_coverage is None
        and evaluation.safety_reasons is None
    )


def _legacy_summary_fields(
    evaluation: PolicyEvaluationRecord,
) -> tuple[object, ...]:
    return (
        evaluation.policy_id,
        evaluation.dataset_identity,
        evaluation.status,
        evaluation.approved,
        evaluation.effective_sample_size,
        evaluation.action_coverage,
    )
