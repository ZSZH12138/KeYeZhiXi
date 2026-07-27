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
from course_insight.modules.m6_tutoring_fsm.features import FeatureBuilder
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    LoadedPolicyArtifact,
)
from course_insight.modules.m6_tutoring_fsm.safety_envelope import SafetyEnvelope


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
            self._prediction(vector, candidate.candidate_id)
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

    def _prediction(
        self,
        features: tuple[float, ...],
        candidate_id: str,
    ) -> PolicyPrediction:
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
        uncertainty = math.sqrt(quadratic)
        score = exploitation + self.alpha * uncertainty
        if not math.isfinite(score):
            raise ValueError("LinUCB score must be finite")
        return PolicyPrediction(
            policy_id=self.policy_id,
            candidate_id=candidate_id,
            score=score,
            propensity=1.0,
            uncertainty=uncertainty,
        )


class LinUCBPolicyAdapter:
    """Production adapter backed by one verified immutable LinUCB artifact."""

    def __init__(
        self,
        loaded_artifact: LoadedPolicyArtifact,
        *,
        exploration_rate: float,
    ) -> None:
        if not isinstance(loaded_artifact, LoadedPolicyArtifact):
            raise TypeError("loaded_artifact must be a LoadedPolicyArtifact")
        _require_probability(exploration_rate, "exploration_rate")
        manifest = loaded_artifact.manifest
        payload = loaded_artifact.payload
        if manifest.algorithm != "linucb":
            raise ValueError("policy artifact algorithm is not supported")
        if manifest.feature_schema_version != FeatureBuilder.schema_version:
            raise ValueError("policy artifact feature schema is not supported")
        if (
            manifest.action_space_version
            != SafetyEnvelope.action_space_version
        ):
            raise ValueError("policy artifact action space is not supported")
        self.policy_id = manifest.policy_id
        self.adapter_id = manifest.adapter_id
        self.adapter_version = manifest.adapter_version
        self._exploration_rate = float(exploration_rate)
        self._feature_builder = FeatureBuilder()
        self._model = LinUCBModel(
            policy_id=manifest.policy_id,
            alpha=payload["alpha"],
            dimension=payload["dimension"],
            actions=payload["actions"],
        )
        if self._model.dimension != len(
            self._feature_builder.build(_dimension_probe_context())
        ):
            raise ValueError("artifact dimension does not match feature schema")

    def select(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyDecision:
        features = self._feature_builder.build(context)
        ranked = self._model.rank(features, candidates)
        scores = {
            prediction.candidate_id: prediction.score
            for prediction in ranked
        }
        uncertainties = {
            prediction.candidate_id: prediction.uncertainty
            for prediction in ranked
        }
        return select_with_epsilon(
            policy_id=self.policy_id,
            request_fingerprint=context.request_fingerprint,
            candidates=candidates,
            scores=scores,
            uncertainties=uncertainties,
            epsilon=self._exploration_rate,
            context=context,
        )


def select_with_epsilon(
    *,
    policy_id: str,
    request_fingerprint: str,
    candidates: Sequence[CandidateAction],
    scores: Mapping[str, float],
    uncertainties: Mapping[str, float],
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
    if set(uncertainties) != set(candidate_ids):
        raise ValueError("uncertainties must match exactly the candidate ids")
    normalized_scores = {candidate_id: _require_finite(scores[candidate_id], "score") for candidate_id in candidate_ids}
    normalized_uncertainties = {
        candidate_id: _require_nonnegative(
            uncertainties[candidate_id],
            "uncertainty",
        )
        for candidate_id in candidate_ids
    }
    winner = max(range(len(ordered)), key=lambda index: normalized_scores[candidate_ids[index]])
    may_explore = len(ordered) > 1 and _exploration_allowed(context, ordered)
    selected_index = winner
    if may_explore and epsilon > 0.0:
        draw = _unit_draw(request_fingerprint)
        if draw < epsilon:
            selected_index = min(len(ordered) - 1, int((draw / epsilon) * len(ordered)))
    probabilities = {
        candidate_id: (1.0 if index == winner else 0.0)
        for index, candidate_id in enumerate(candidate_ids)
    }
    if may_explore and epsilon > 0.0:
        probabilities = {
            candidate_id: (
                (1.0 - epsilon) + epsilon / len(ordered)
                if index == winner
                else epsilon / len(ordered)
            )
            for index, candidate_id in enumerate(candidate_ids)
        }
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
            propensity=probabilities[selected_id],
            uncertainty=normalized_uncertainties[selected_id],
            action_probabilities=tuple(probabilities.items()),
            model_scores=tuple(normalized_scores.items()),
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
    if _require_nonnegative(value, name) < 0.0:
        raise ValueError(f"{name} must be non-negative")


def _require_nonnegative(value: Any, name: str) -> float:
    numeric = _require_finite(value, name)
    if numeric < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return numeric


def _require_probability(value: Any, name: str) -> None:
    numeric = _require_finite(value, name)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"{name} must be a probability")


def _dimension_probe_context() -> TutoringPolicyContext:
    from course_insight.modules.m6_tutoring_fsm.decision_policy import (
        DecisionSignals,
    )

    return TutoringPolicyContext(
        request_fingerprint="dimension-probe",
        current_state="S0",
        task_type="qa",
        turn_count=0,
        score_ratio=0.0,
        target_concept_count=0,
        signals=DecisionSignals(
            needs_teacher_review=False,
            has_diagnosed_misconception=False,
            has_active_misconception=False,
            has_prerequisite_gap=False,
            has_new_evidence=False,
            minimum_recent_correction_rate=0.0,
            minimum_mastery_confidence=0.0,
            maximum_hint_dependency=0.0,
        ),
        learner_evidence_count=0,
    )
