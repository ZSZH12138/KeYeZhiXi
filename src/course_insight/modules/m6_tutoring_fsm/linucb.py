"""Finite-only, deterministic pure-Python LinUCB scoring and exploration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import math
from typing import Any

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    PolicyDecision,
    PolicyPrediction,
    TutoringPolicyContext,
)


@dataclass(frozen=True, slots=True)
class _ActionParameters:
    theta: tuple[float, ...]
    inverse_covariance: tuple[tuple[float, ...], ...]


class LinUCBModel:
    """A validated immutable action model with stable candidate ordering."""

    def __init__(
        self,
        *,
        policy_id: str,
        alpha: float,
        dimension: int,
        actions: Mapping[str, Mapping[str, Any]],
    ) -> None:
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must not be blank")
        if type(dimension) is not int or dimension <= 0:
            raise ValueError("dimension must be a positive integer")
        _require_finite_nonnegative(alpha, "alpha")
        if not actions:
            raise ValueError("actions must not be empty")
        parsed: dict[str, _ActionParameters] = {}
        for candidate_id, value in actions.items():
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise ValueError("action candidate id must not be blank")
            if not isinstance(value, Mapping):
                raise ValueError("action parameters must be an object")
            parsed[candidate_id] = _parse_parameters(value, dimension)
        self.policy_id = policy_id
        self.alpha = float(alpha)
        self.dimension = dimension
        self._actions = parsed

    def rank(
        self,
        features: Sequence[float],
        candidates: Sequence[CandidateAction],
    ) -> tuple[PolicyPrediction, ...]:
        """Score each supplied candidate in its supplied order."""

        vector = _validate_features(features, self.dimension)
        ordered = tuple(candidates)
        if not ordered:
            raise ValueError("LinUCB requires at least one candidate")
        predictions = tuple(
            PolicyPrediction(
                policy_id=self.policy_id,
                candidate_id=candidate.candidate_id,
                score=self._score(vector, candidate.candidate_id),
                propensity=1.0,
            )
            for candidate in ordered
        )
        return tuple(sorted(predictions, key=lambda prediction: -prediction.score))

    def choose(
        self,
        features: Sequence[float],
        candidates: Sequence[CandidateAction],
    ) -> PolicyPrediction:
        """Return the greatest score; Python's stable sort preserves ties."""

        return self.rank(features, candidates)[0]

    def _score(self, features: tuple[float, ...], candidate_id: str) -> float:
        parameters = self._actions.get(candidate_id)
        if parameters is None:
            raise ValueError("candidate has no LinUCB action parameters")
        exploitation = sum(theta * value for theta, value in zip(parameters.theta, features))
        quadratic = sum(
            features[row] * parameters.inverse_covariance[row][column] * features[column]
            for row in range(self.dimension)
            for column in range(self.dimension)
        )
        if not math.isfinite(exploitation) or not math.isfinite(quadratic):
            raise ValueError("LinUCB score must be finite")
        if quadratic < 0.0:
            if quadratic > -1e-12:
                quadratic = 0.0
            else:
                raise ValueError("inverse covariance produced a negative uncertainty")
        score = exploitation + self.alpha * math.sqrt(quadratic)
        if not math.isfinite(score):
            raise ValueError("LinUCB score must be finite")
        return score


def select_with_epsilon(
    *,
    policy_id: str,
    request_fingerprint: str,
    candidates: Sequence[CandidateAction],
    scores: Mapping[str, float],
    epsilon: float,
    context: TutoringPolicyContext,
) -> PolicyDecision:
    """Apply deterministic SHA-256 epsilon exploration and exact propensity."""

    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ValueError("policy_id must not be blank")
    if request_fingerprint != context.request_fingerprint:
        raise ValueError("request fingerprint must match policy context")
    _require_probability(epsilon, "epsilon")
    ordered = tuple(candidates)
    if not ordered:
        raise ValueError("epsilon selection requires at least one candidate")
    candidate_ids = tuple(candidate.candidate_id for candidate in ordered)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate ids must be unique")
    if set(scores) != set(candidate_ids):
        raise ValueError("scores must match exactly the candidate ids")
    normalized_scores = {candidate_id: _require_finite(scores[candidate_id], "score") for candidate_id in candidate_ids}
    winner = max(range(len(ordered)), key=lambda index: normalized_scores[candidate_ids[index]])
    may_explore = len(ordered) > 1 and _exploration_allowed(context, ordered)
    selected_index = winner
    if may_explore and epsilon > 0.0:
        draw = _unit_draw(request_fingerprint)
        if draw < epsilon:
            selected_index = min(len(ordered) - 1, int((draw / epsilon) * len(ordered)))
    propensity = 1.0
    if may_explore and epsilon > 0.0:
        propensity = (1.0 - epsilon) + epsilon / len(ordered) if selected_index == winner else epsilon / len(ordered)
    selected_id = candidate_ids[selected_index]
    return PolicyDecision(
        request_fingerprint=request_fingerprint,
        mode="active",
        selected_candidate_id=selected_id,
        candidate_ids=candidate_ids,
        prediction=PolicyPrediction(
            policy_id=policy_id,
            candidate_id=selected_id,
            score=normalized_scores[selected_id],
            propensity=propensity,
        ),
    )


def _parse_parameters(value: Mapping[str, Any], dimension: int) -> _ActionParameters:
    if set(value) != {"theta", "inverse_covariance"}:
        raise ValueError("action parameters require theta and inverse_covariance only")
    theta = _validate_features(value["theta"], dimension)
    matrix_value = value["inverse_covariance"]
    if not isinstance(matrix_value, Sequence) or isinstance(matrix_value, (str, bytes)) or len(matrix_value) != dimension:
        raise ValueError("inverse_covariance has invalid dimensions")
    matrix = tuple(_validate_features(row, dimension) for row in matrix_value)
    return _ActionParameters(theta=theta, inverse_covariance=matrix)


def _validate_features(values: Any, dimension: int) -> tuple[float, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) != dimension:
        raise ValueError("feature vector has invalid dimensions")
    return tuple(_require_finite(value, "feature value") for value in values)


def _exploration_allowed(context: TutoringPolicyContext, candidates: tuple[CandidateAction, ...]) -> bool:
    signals = context.signals
    return (
        all(candidate.exploration_allowed for candidate in candidates)
        and not signals.needs_teacher_review
        and not signals.has_diagnosed_misconception
        and not signals.has_active_misconception
        and not signals.has_prerequisite_gap
    )


def _unit_draw(request_fingerprint: str) -> float:
    integer = int.from_bytes(sha256(request_fingerprint.encode("utf-8")).digest(), "big")
    return integer / (1 << 256)


def _require_finite(value: Any, name: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _require_finite_nonnegative(value: Any, name: str) -> None:
    if _require_finite(value, name) < 0.0:
        raise ValueError(f"{name} must be non-negative")


def _require_probability(value: Any, name: str) -> None:
    numeric = _require_finite(value, name)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{name} must be a probability")
