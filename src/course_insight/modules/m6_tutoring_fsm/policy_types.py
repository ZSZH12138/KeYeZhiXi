"""Private immutable domain values for M6 policy learning.

These values deliberately stay outside the public Pydantic contracts.  Their
identities are content-addressed with canonical JSON so a policy decision can
be reproduced without relying on process-local state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any, ClassVar, Mapping

from course_insight.modules.m6_tutoring_fsm.decision_policy import DecisionSignals


TUTORING_STATES: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4", "S5")
TASK_TYPES: tuple[str, ...] = (
    "qa",
    "diagnostic",
    "practice",
    "correction",
    "stage_assessment",
)
POLICY_MODES: tuple[str, ...] = ("rules", "shadow", "active")
ARTIFACT_STATUSES: tuple[str, ...] = (
    "draft",
    "shadow",
    "approved",
    "rejected",
    "retired",
)
EVALUATION_METRICS: tuple[str, ...] = ("ips", "snips", "dm", "dr")
EVALUATION_SAFETY_REASONS: tuple[str, ...] = (
    "insufficient_rows",
    "invalid_propensity",
    "low_support",
    "low_effective_sample_size",
    "poor_action_coverage",
    "reward_below_approval_threshold",
)
_SAFE_AUDIT_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
)
_SECRET_LIKE_AUDIT_IDENTIFIER = re.compile(
    r"^(?:sk-(?:proj-)?|gh[opurs]_|xox[baprs]-|AKIA|AIza)",
    re.IGNORECASE,
)


class _CanonicalIdentity:
    """Provide UTF-8, sorted-key compact JSON and a lowercase SHA-256 ID."""

    __slots__ = ()

    def canonical_payload(self) -> Mapping[str, Any]:
        raise NotImplementedError

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
class CandidateAction(_CanonicalIdentity):
    """One state-machine-safe action which an adapter may rank, but not alter."""

    candidate_id: str
    next_state: str
    action_type: str
    prompt_template_id: str
    exploration_allowed: bool

    def __post_init__(self) -> None:
        _require_nonblank(self.candidate_id, "candidate_id")
        _require_member(self.next_state, TUTORING_STATES, "next_state")
        _require_nonblank(self.action_type, "action_type")
        _require_nonblank(self.prompt_template_id, "prompt_template_id")
        _require_bool(self.exploration_allowed, "exploration_allowed")

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "next_state": self.next_state,
            "action_type": self.action_type,
            "prompt_template_id": self.prompt_template_id,
            "exploration_allowed": self.exploration_allowed,
        }


@dataclass(frozen=True, slots=True)
class TutoringPolicyContext(_CanonicalIdentity):
    """The structured, non-sensitive inputs available to policy code."""

    request_fingerprint: str
    current_state: str
    task_type: str
    turn_count: int
    score_ratio: float
    target_concept_count: int
    signals: DecisionSignals
    learner_evidence_count: int
    course_id: str | None = None
    class_id: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.request_fingerprint, "request_fingerprint")
        _require_member(self.current_state, TUTORING_STATES, "current_state")
        _require_member(self.task_type, TASK_TYPES, "task_type")
        _require_nonnegative_int(self.turn_count, "turn_count")
        _require_probability(self.score_ratio, "score_ratio")
        object.__setattr__(self, "score_ratio", float(self.score_ratio))
        _require_nonnegative_int(self.target_concept_count, "target_concept_count")
        if not isinstance(self.signals, DecisionSignals):
            raise ValueError("signals must be a DecisionSignals instance")
        _require_nonnegative_int(self.learner_evidence_count, "learner_evidence_count")
        for name in ("course_id", "class_id"):
            value = getattr(self, name)
            if value is not None:
                _require_nonblank(value, name)

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "request_fingerprint": self.request_fingerprint,
            "current_state": self.current_state,
            "task_type": self.task_type,
            "turn_count": self.turn_count,
            "score_ratio": self.score_ratio,
            "target_concept_count": self.target_concept_count,
            "signals": _signals_payload(self.signals),
            "learner_evidence_count": self.learner_evidence_count,
            "course_id": self.course_id,
            "class_id": self.class_id,
        }


@dataclass(frozen=True, slots=True)
class PolicyPrediction(_CanonicalIdentity):
    """A policy score and its actual selection propensity for one candidate."""

    policy_id: str
    candidate_id: str
    score: float
    propensity: float
    uncertainty: float
    feature_schema_version: str = "m6-features-v1"
    action_probabilities: object = None
    model_scores: object = None

    def __post_init__(self) -> None:
        _require_nonblank(self.policy_id, "policy_id")
        _require_nonblank(self.candidate_id, "candidate_id")
        _require_finite(self.score, "score")
        _require_probability(self.propensity, "propensity")
        _require_finite(self.uncertainty, "uncertainty")
        if self.uncertainty < 0.0:
            raise ValueError("uncertainty must be non-negative")
        object.__setattr__(self, "score", float(self.score))
        object.__setattr__(self, "propensity", float(self.propensity))
        object.__setattr__(self, "uncertainty", float(self.uncertainty))
        _require_nonblank(self.feature_schema_version, "feature_schema_version")
        if self.action_probabilities is None and self.model_scores is None:
            return
        if self.action_probabilities is None or self.model_scores is None:
            raise ValueError(
                "action_probabilities and model_scores must be supplied together"
            )
        probabilities = _normalize_action_measurements(
            self.action_probabilities,
            "action_probabilities",
            probability=True,
            allow_empty=False,
        )
        scores = _normalize_action_measurements(
            self.model_scores,
            "model_scores",
            probability=False,
            allow_empty=False,
        )
        if {key for key, _ in probabilities} != {key for key, _ in scores}:
            raise ValueError(
                "action_probabilities and model_scores must cover the same actions"
            )
        if self.candidate_id not in {key for key, _ in probabilities}:
            raise ValueError("prediction candidate must have an action probability")
        if not math.isclose(
            sum(value for _, value in probabilities),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("action_probabilities must sum to one")
        selected_probability = dict(probabilities)[self.candidate_id]
        if not math.isclose(
            selected_probability,
            self.propensity,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("propensity must equal the selected action probability")
        object.__setattr__(self, "action_probabilities", probabilities)
        object.__setattr__(self, "model_scores", scores)

    def canonical_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "policy_id": self.policy_id,
            "candidate_id": self.candidate_id,
            "score": self.score,
            "propensity": self.propensity,
            "uncertainty": self.uncertainty,
            "feature_schema_version": self.feature_schema_version,
        }
        if self.action_probabilities is not None:
            payload["action_probabilities"] = dict(self.action_probabilities)
            payload["model_scores"] = dict(self.model_scores)
        return payload


@dataclass(frozen=True, slots=True)
class PolicyDecision(_CanonicalIdentity):
    """An immutable selected candidate and the candidate set it came from."""

    request_fingerprint: str
    mode: str
    selected_candidate_id: str
    candidate_ids: tuple[str, ...]
    prediction: PolicyPrediction | None
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.request_fingerprint, "request_fingerprint")
        _require_member(self.mode, POLICY_MODES, "mode")
        _require_nonblank(self.selected_candidate_id, "selected_candidate_id")
        _require_nonempty_unique_strings(self.candidate_ids, "candidate_ids")
        if self.selected_candidate_id not in self.candidate_ids:
            raise ValueError("selected_candidate_id must be a candidate")
        if self.prediction is not None and not isinstance(
            self.prediction, PolicyPrediction
        ):
            raise ValueError("prediction must be a PolicyPrediction or None")
        if (
            self.prediction is not None
            and self.prediction.candidate_id != self.selected_candidate_id
        ):
            raise ValueError("prediction.candidate_id must equal selected_candidate_id")
        if self.fallback_reason is not None:
            _require_nonblank(self.fallback_reason, "fallback_reason")

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "request_fingerprint": self.request_fingerprint,
            "mode": self.mode,
            "selected_candidate_id": self.selected_candidate_id,
            "candidate_ids": list(self.candidate_ids),
            "prediction": (
                None if self.prediction is None else self.prediction.canonical_payload()
            ),
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True, slots=True)
class PolicyExecutionRef(_CanonicalIdentity):
    """First-writer policy binding for one existing M6 request fingerprint."""

    request_fingerprint: str
    mode: str
    policy_id: str
    adapter_id: str
    adapter_version: str
    artifact_sha256: str | None
    feature_schema_version: str
    action_space_version: str
    gate_policy_version: str
    input_fingerprint: str | None = None
    exploration_rate: float | None = None
    active_gate_allowed: bool | None = None
    active_gate_reasons: object = None

    def __post_init__(self) -> None:
        _require_nonblank(self.request_fingerprint, "request_fingerprint")
        _require_member(self.mode, POLICY_MODES, "mode")
        for name in (
            "policy_id",
            "adapter_id",
            "adapter_version",
            "feature_schema_version",
            "action_space_version",
            "gate_policy_version",
        ):
            _require_nonblank(getattr(self, name), name)
        if self.artifact_sha256 is not None:
            _require_sha256(self.artifact_sha256, "artifact_sha256")
        if self.input_fingerprint is not None:
            _require_sha256(self.input_fingerprint, "input_fingerprint")
        if self.exploration_rate is not None:
            _require_probability(self.exploration_rate, "exploration_rate")
            object.__setattr__(
                self,
                "exploration_rate",
                float(self.exploration_rate),
            )
        reasons = _normalize_string_sequence(
            () if self.active_gate_reasons is None else self.active_gate_reasons,
            "active_gate_reasons",
        )
        if self.active_gate_allowed is not None:
            _require_bool(self.active_gate_allowed, "active_gate_allowed")
            if self.mode != "active":
                raise ValueError(
                    "active gate snapshot requires active execution mode"
                )
            if self.active_gate_allowed and reasons:
                raise ValueError(
                    "allowed active gate snapshot must not have reasons"
                )
            if not self.active_gate_allowed and not reasons:
                raise ValueError(
                    "rejected active gate snapshot requires reasons"
                )
        elif reasons:
            raise ValueError(
                "active gate reasons require an active gate snapshot"
            )
        object.__setattr__(self, "active_gate_reasons", reasons)

    @property
    def policy_execution_fingerprint(self) -> str:
        if self.input_fingerprint is None:
            return self.identity
        ordered_identity = (
            self.input_fingerprint,
            self.adapter_id,
            self.adapter_version,
            self.artifact_sha256,
            self.feature_schema_version,
            self.action_space_version,
            self.gate_policy_version,
        )
        payload = json.dumps(
            ordered_identity,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def canonical_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "request_fingerprint": self.request_fingerprint,
            "mode": self.mode,
            "policy_id": self.policy_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "artifact_sha256": self.artifact_sha256,
            "feature_schema_version": self.feature_schema_version,
            "action_space_version": self.action_space_version,
            "gate_policy_version": self.gate_policy_version,
        }
        if self.input_fingerprint is not None:
            payload["input_fingerprint"] = self.input_fingerprint
        if self.exploration_rate is not None:
            payload["exploration_rate"] = self.exploration_rate
        if self.active_gate_allowed is not None:
            payload.update(
                {
                    "active_gate_allowed": self.active_gate_allowed,
                    "active_gate_reasons": list(self.active_gate_reasons),
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class PolicyArtifactManifest(_CanonicalIdentity):
    """Validated metadata for a JSON-only policy artifact."""

    policy_id: str
    adapter_id: str
    adapter_version: str
    algorithm: str
    state_graph_version: str
    baseline_policy_version: str
    feature_schema_version: str
    action_space_version: str
    reward_version: str
    gate_policy_version: str
    training_data_watermark: str
    training_data_checksum: str
    artifact_sha256: str
    status: str
    created_at: str
    artifact_reference: str
    allowed_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "adapter_id",
            "adapter_version",
            "algorithm",
            "state_graph_version",
            "baseline_policy_version",
            "feature_schema_version",
            "action_space_version",
            "reward_version",
            "gate_policy_version",
            "training_data_watermark",
            "created_at",
            "artifact_reference",
        ):
            _require_nonblank(getattr(self, name), name)
        _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_sha256(self.training_data_checksum, "training_data_checksum")
        _require_member(self.status, ARTIFACT_STATUSES, "status")
        _require_relative_artifact_reference(self.artifact_reference)
        _require_nonempty_unique_strings(self.allowed_scopes, "allowed_scopes")

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "policy_id": self.policy_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "algorithm": self.algorithm,
            "state_graph_version": self.state_graph_version,
            "baseline_policy_version": self.baseline_policy_version,
            "feature_schema_version": self.feature_schema_version,
            "action_space_version": self.action_space_version,
            "reward_version": self.reward_version,
            "gate_policy_version": self.gate_policy_version,
            "training_data_watermark": self.training_data_watermark,
            "training_data_checksum": self.training_data_checksum,
            "artifact_sha256": self.artifact_sha256,
            "status": self.status,
            "created_at": self.created_at,
            "artifact_reference": self.artifact_reference,
            "allowed_scopes": list(self.allowed_scopes),
        }


@dataclass(frozen=True, slots=True)
class PolicyGateResult(_CanonicalIdentity):
    """Fail-closed result of all active-mode gate checks."""

    allowed: bool
    reasons: tuple[str, ...]
    gate_policy_version: str

    def __post_init__(self) -> None:
        _require_bool(self.allowed, "allowed")
        _require_unique_strings(self.reasons, "reasons")
        _require_nonblank(self.gate_policy_version, "gate_policy_version")
        if self.allowed and self.reasons:
            raise ValueError("allowed gate result must not contain rejection reasons")
        if not self.allowed and not self.reasons:
            raise ValueError("rejected gate result requires a reason")

    def canonical_payload(self) -> Mapping[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "gate_policy_version": self.gate_policy_version,
        }


@dataclass(frozen=True, slots=True)
class PolicyObservation(_CanonicalIdentity):
    """The private, de-identified record needed for later policy evaluation."""

    policy_execution_fingerprint: str
    request_fingerprint: str
    feature_schema_version: str
    candidate_ids: tuple[str, ...]
    selected_candidate_id: str
    propensity: float
    decision_id: str | None = None
    input_fingerprint: str | None = None
    context_checksum: str | None = None
    candidate_set_checksum: str | None = None
    baseline_action_id: str | None = None
    chosen_action_id: str | None = None
    action_probabilities: object = None
    model_scores: object = None
    uncertainty: float | None = None
    decision_source: str | None = None
    shadow_action_id: str | None = None
    reason_codes: object = None
    policy_id: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    artifact_sha256: str | None = None
    action_space_version: str | None = None
    gate_policy_version: str | None = None
    logging_policy_id: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "policy_execution_fingerprint",
            "request_fingerprint",
            "feature_schema_version",
            "selected_candidate_id",
        ):
            _require_nonblank(getattr(self, name), name)
        _require_nonempty_unique_strings(self.candidate_ids, "candidate_ids")
        if self.selected_candidate_id not in self.candidate_ids:
            raise ValueError("selected_candidate_id must be a candidate")
        _require_probability(self.propensity, "propensity")
        object.__setattr__(self, "propensity", float(self.propensity))
        if self.decision_id is None:
            rich_values = (
                self.input_fingerprint,
                self.context_checksum,
                self.candidate_set_checksum,
                self.baseline_action_id,
                self.chosen_action_id,
                self.action_probabilities,
                self.model_scores,
                self.uncertainty,
                self.decision_source,
                self.shadow_action_id,
                self.reason_codes,
                self.policy_id,
                self.adapter_id,
                self.adapter_version,
                self.artifact_sha256,
                self.action_space_version,
                self.gate_policy_version,
                self.logging_policy_id,
                self.created_at,
            )
            if any(value is not None for value in rich_values):
                raise ValueError("rich observation fields require decision_id")
            return
        for name in (
            "decision_id",
            "input_fingerprint",
            "context_checksum",
            "candidate_set_checksum",
            "baseline_action_id",
            "chosen_action_id",
            "decision_source",
            "policy_id",
            "adapter_id",
            "adapter_version",
            "action_space_version",
            "gate_policy_version",
            "logging_policy_id",
            "created_at",
        ):
            _require_nonblank(getattr(self, name), name)
        _require_sha256(self.input_fingerprint, "input_fingerprint")
        _require_sha256(self.context_checksum, "context_checksum")
        _require_sha256(self.candidate_set_checksum, "candidate_set_checksum")
        if self.artifact_sha256 is not None:
            _require_sha256(self.artifact_sha256, "artifact_sha256")
        _require_member(
            self.decision_source,
            ("rules", "shadow_baseline", "active_policy", "fallback"),
            "decision_source",
        )
        if self.baseline_action_id not in self.candidate_ids:
            raise ValueError("baseline_action_id must be a candidate")
        if self.chosen_action_id not in self.candidate_ids:
            raise ValueError("chosen_action_id must be a candidate")
        if self.selected_candidate_id != self.chosen_action_id:
            raise ValueError("selected_candidate_id must equal chosen_action_id")
        if (
            self.shadow_action_id is not None
            and self.shadow_action_id not in self.candidate_ids
        ):
            raise ValueError("shadow_action_id must be a candidate")
        probabilities = _normalize_action_measurements(
            self.action_probabilities,
            "action_probabilities",
            probability=True,
            allow_empty=False,
        )
        if {key for key, _ in probabilities} != set(self.candidate_ids):
            raise ValueError("action_probabilities must cover every candidate")
        if not math.isclose(
            sum(value for _, value in probabilities),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("action_probabilities must sum to one")
        if not math.isclose(
            dict(probabilities)[self.chosen_action_id],
            self.propensity,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("propensity must equal chosen action probability")
        scores = _normalize_action_measurements(
            self.model_scores,
            "model_scores",
            probability=False,
            allow_empty=True,
        )
        if scores and {key for key, _ in scores} != set(self.candidate_ids):
            raise ValueError("model_scores must cover every candidate or be empty")
        if self.uncertainty is not None:
            _require_finite(self.uncertainty, "uncertainty")
            if self.uncertainty < 0.0:
                raise ValueError("uncertainty must be non-negative")
            object.__setattr__(self, "uncertainty", float(self.uncertainty))
        reasons = _normalize_string_sequence(
            () if self.reason_codes is None else self.reason_codes,
            "reason_codes",
        )
        _require_timezone_string(self.created_at, "created_at")
        object.__setattr__(self, "action_probabilities", probabilities)
        object.__setattr__(self, "model_scores", scores)
        object.__setattr__(self, "reason_codes", reasons)

    def canonical_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "policy_execution_fingerprint": self.policy_execution_fingerprint,
            "request_fingerprint": self.request_fingerprint,
            "feature_schema_version": self.feature_schema_version,
            "candidate_ids": list(self.candidate_ids),
            "selected_candidate_id": self.selected_candidate_id,
            "propensity": self.propensity,
        }
        if self.decision_id is not None:
            payload.update(
                {
                    "decision_id": self.decision_id,
                    "input_fingerprint": self.input_fingerprint,
                    "context_checksum": self.context_checksum,
                    "candidate_set_checksum": self.candidate_set_checksum,
                    "baseline_action_id": self.baseline_action_id,
                    "chosen_action_id": self.chosen_action_id,
                    "action_probabilities": dict(self.action_probabilities),
                    "model_scores": dict(self.model_scores),
                    "uncertainty": self.uncertainty,
                    "decision_source": self.decision_source,
                    "shadow_action_id": self.shadow_action_id,
                    "reason_codes": list(self.reason_codes),
                    "policy_id": self.policy_id,
                    "adapter_id": self.adapter_id,
                    "adapter_version": self.adapter_version,
                    "artifact_sha256": self.artifact_sha256,
                    "action_space_version": self.action_space_version,
                    "gate_policy_version": self.gate_policy_version,
                    "logging_policy_id": self.logging_policy_id,
                    "created_at": self.created_at,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class PolicyOutcome(_CanonicalIdentity):
    """A later outcome; missing follow-up remains explicit rather than zeroed."""

    policy_execution_fingerprint: str
    status: str
    transfer_success: float | None
    hint_count: int
    loop_count: int
    independent_correction_success: bool | None = None
    self_explanation_passed: bool | None = None
    additional_turn_count: int | None = None
    teacher_review_escalated: bool | None = None
    safety_flag: bool | None = None
    outcome_event_ids: object = None
    outcome_watermark: str | None = None
    observed_at: str | None = None

    _STATUSES: ClassVar[tuple[str, ...]] = (
        "pending",
        "censored",
        "observed",
        "invalid",
    )

    def __post_init__(self) -> None:
        _require_nonblank(
            self.policy_execution_fingerprint, "policy_execution_fingerprint"
        )
        _require_member(self.status, self._STATUSES, "status")
        _require_nonnegative_int(self.hint_count, "hint_count")
        _require_nonnegative_int(self.loop_count, "loop_count")
        if self.status == "observed":
            if self.transfer_success is None:
                raise ValueError("observed outcome requires transfer_success")
            _require_probability(self.transfer_success, "transfer_success")
            object.__setattr__(self, "transfer_success", float(self.transfer_success))
        elif self.status in {"pending", "censored"} and self.transfer_success is not None:
            raise ValueError("pending or censored outcome must not set transfer_success")
        elif self.transfer_success is not None:
            _require_probability(self.transfer_success, "transfer_success")
            object.__setattr__(self, "transfer_success", float(self.transfer_success))
        for name in (
            "independent_correction_success",
            "self_explanation_passed",
            "teacher_review_escalated",
            "safety_flag",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_bool(value, name)
        if self.additional_turn_count is not None:
            _require_nonnegative_int(
                self.additional_turn_count,
                "additional_turn_count",
            )
        if self.safety_flag is True and self.status != "invalid":
            raise ValueError("safety_flag requires an invalid outcome")
        if self.outcome_event_ids is not None:
            object.__setattr__(
                self,
                "outcome_event_ids",
                _normalize_audit_identifiers(
                    self.outcome_event_ids,
                    "outcome_event_ids",
                ),
            )
        if self.outcome_watermark is not None:
            _require_safe_audit_identifier(
                self.outcome_watermark,
                "outcome_watermark",
            )
        if self.observed_at is not None:
            _require_timezone_string(self.observed_at, "observed_at")
        if (
            self.has_audit_details
            and self.status in {"observed", "censored", "invalid"}
            and self.observed_at is None
        ):
            raise ValueError("audited outcome requires timezone observed_at")

    @property
    def has_audit_details(self) -> bool:
        return any(
            value is not None
            for value in (
                self.independent_correction_success,
                self.self_explanation_passed,
                self.additional_turn_count,
                self.teacher_review_escalated,
                self.safety_flag,
                self.outcome_event_ids,
                self.outcome_watermark,
                self.observed_at,
            )
        )

    def canonical_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "policy_execution_fingerprint": self.policy_execution_fingerprint,
            "status": self.status,
            "transfer_success": self.transfer_success,
            "hint_count": self.hint_count,
            "loop_count": self.loop_count,
        }
        if self.has_audit_details:
            payload.update(
                {
                    "independent_correction_success": (
                        self.independent_correction_success
                    ),
                    "self_explanation_passed": self.self_explanation_passed,
                    "additional_hint_count": self.hint_count,
                    "additional_turn_count": self.additional_turn_count,
                    "teacher_review_escalated": self.teacher_review_escalated,
                    "safety_flag": self.safety_flag,
                    "outcome_event_ids": (
                        []
                        if self.outcome_event_ids is None
                        else list(self.outcome_event_ids)
                    ),
                    "outcome_watermark": self.outcome_watermark,
                    "observed_at": self.observed_at,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class PolicyRewardRecord(_CanonicalIdentity):
    """A versioned reward result associated with one immutable outcome."""

    policy_execution_fingerprint: str
    outcome_identity: str
    status: str
    reward: float | None
    reward_version: str = "m6-reward-v1"
    transfer_success: float | None = None
    independent_correction_success: bool | None = None
    self_explanation_passed: bool | None = None
    additional_hint_count: int | None = None
    additional_turn_count: int | None = None
    loop_count: int | None = None
    teacher_review_escalated: bool | None = None
    safety_flag: bool | None = None
    outcome_event_ids: object = None
    outcome_watermark: str | None = None
    observed_at: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(
            self.policy_execution_fingerprint, "policy_execution_fingerprint"
        )
        _require_nonblank(self.outcome_identity, "outcome_identity")
        _require_member(
            self.status,
            ("pending", "censored", "observed", "invalid"),
            "status",
        )
        _require_nonblank(self.reward_version, "reward_version")
        if self.status == "observed":
            if self.reward is None:
                raise ValueError("observed reward record requires reward")
            _require_finite(self.reward, "reward")
            object.__setattr__(self, "reward", float(self.reward))
        elif self.reward is not None:
            raise ValueError(
                "pending, censored, or invalid reward record must not set reward"
            )
        if self.transfer_success is not None:
            _require_probability(self.transfer_success, "transfer_success")
            object.__setattr__(self, "transfer_success", float(self.transfer_success))
        for name in (
            "independent_correction_success",
            "self_explanation_passed",
            "teacher_review_escalated",
            "safety_flag",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_bool(value, name)
        for name in (
            "additional_hint_count",
            "additional_turn_count",
            "loop_count",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_nonnegative_int(value, name)
        if self.safety_flag is True and self.status != "invalid":
            raise ValueError("safety_flag requires an invalid reward record")
        if self.outcome_event_ids is not None:
            object.__setattr__(
                self,
                "outcome_event_ids",
                _normalize_audit_identifiers(
                    self.outcome_event_ids,
                    "outcome_event_ids",
                ),
            )
        if self.outcome_watermark is not None:
            _require_safe_audit_identifier(
                self.outcome_watermark,
                "outcome_watermark",
            )
        if self.observed_at is not None:
            _require_timezone_string(self.observed_at, "observed_at")

    def canonical_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "policy_execution_fingerprint": self.policy_execution_fingerprint,
            "outcome_identity": self.outcome_identity,
            "status": self.status,
            "reward": self.reward,
            "reward_version": self.reward_version,
        }
        if self.has_raw_outcome:
            payload.update(
                {
                    "transfer_success": self.transfer_success,
                    "independent_correction_success": (
                        self.independent_correction_success
                    ),
                    "self_explanation_passed": self.self_explanation_passed,
                    "additional_hint_count": self.additional_hint_count,
                    "additional_turn_count": self.additional_turn_count,
                    "loop_count": self.loop_count,
                    "teacher_review_escalated": self.teacher_review_escalated,
                    "safety_flag": self.safety_flag,
                    "outcome_event_ids": (
                        []
                        if self.outcome_event_ids is None
                        else list(self.outcome_event_ids)
                    ),
                    "outcome_watermark": self.outcome_watermark,
                    "observed_at": self.observed_at,
                }
            )
        return payload

    @property
    def has_raw_outcome(self) -> bool:
        return any(
            value is not None
            for value in (
                self.transfer_success,
                self.independent_correction_success,
                self.self_explanation_passed,
                self.additional_hint_count,
                self.additional_turn_count,
                self.loop_count,
                self.teacher_review_escalated,
                self.safety_flag,
                self.outcome_event_ids,
                self.outcome_watermark,
                self.observed_at,
            )
        )


@dataclass(frozen=True, slots=True)
class PolicyEvaluationRecord(_CanonicalIdentity):
    """Private, finite summary of a deterministic offline policy evaluation."""

    policy_id: str
    dataset_identity: str
    status: str
    approved: bool
    effective_sample_size: float
    action_coverage: float
    observation_count: int
    metrics: object = ()
    confidence_intervals: object = ()
    state_slices: object = ()
    group_slices: object = ()
    support_coverage: float | None = None
    safety_reasons: object = None

    def __post_init__(self) -> None:
        _require_nonblank(self.policy_id, "policy_id")
        _require_nonblank(self.dataset_identity, "dataset_identity")
        _require_member(self.status, ("sufficient_data", "insufficient_data"), "status")
        _require_bool(self.approved, "approved")
        _require_finite(self.effective_sample_size, "effective_sample_size")
        object.__setattr__(
            self,
            "effective_sample_size",
            float(self.effective_sample_size),
        )
        if self.effective_sample_size < 0.0:
            raise ValueError("effective_sample_size must be non-negative")
        _require_probability(self.action_coverage, "action_coverage")
        object.__setattr__(self, "action_coverage", float(self.action_coverage))
        _require_nonnegative_int(self.observation_count, "observation_count")
        object.__setattr__(
            self,
            "metrics",
            _normalize_metric_entries(self.metrics, "metrics"),
        )
        _require_evaluation_metric_names(self.metrics, "metrics")
        object.__setattr__(
            self,
            "confidence_intervals",
            _normalize_confidence_intervals(
                self.confidence_intervals,
                "confidence_intervals",
            ),
        )
        _require_evaluation_metric_names(
            tuple(
                (name, lower)
                for name, lower, _ in self.confidence_intervals
            ),
            "confidence_intervals",
        )
        object.__setattr__(
            self,
            "state_slices",
            _normalize_evaluation_slices(self.state_slices, "state_slices"),
        )
        if any(
            key not in TUTORING_STATES
            for key, _, _ in self.state_slices
        ):
            raise ValueError("state_slices contains an unsupported state")
        object.__setattr__(
            self,
            "group_slices",
            _normalize_evaluation_slices(self.group_slices, "group_slices"),
        )
        for key, _, _ in self.group_slices:
            _require_sha256(key, "group_slices")
        if self.support_coverage is not None:
            _require_probability(self.support_coverage, "support_coverage")
            object.__setattr__(
                self,
                "support_coverage",
                float(self.support_coverage),
            )
        if self.safety_reasons is not None:
            reasons = _normalize_string_sequence(
                self.safety_reasons,
                "safety_reasons",
            )
            if any(
                reason not in EVALUATION_SAFETY_REASONS
                for reason in reasons
            ):
                raise ValueError(
                    "safety_reasons contains an unsupported reason"
                )
            object.__setattr__(self, "safety_reasons", reasons)
        if self.status == "insufficient_data" and self.approved:
            raise ValueError("insufficient_data evaluation cannot be approved")

    def canonical_payload(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "policy_id": self.policy_id,
            "dataset_identity": self.dataset_identity,
            "status": self.status,
            "approved": self.approved,
            "effective_sample_size": self.effective_sample_size,
            "action_coverage": self.action_coverage,
            "observation_count": self.observation_count,
        }
        if self.metrics:
            payload["metrics"] = dict(self.metrics)
        if self.confidence_intervals:
            payload["confidence_intervals"] = {
                name: [lower, upper]
                for name, lower, upper in self.confidence_intervals
            }
        if self.state_slices:
            payload["state_slices"] = _evaluation_slices_payload(
                self.state_slices
            )
        if self.group_slices:
            payload["group_slices"] = _evaluation_slices_payload(
                self.group_slices
            )
        if self.support_coverage is not None:
            payload["support_coverage"] = self.support_coverage
        if self.safety_reasons is not None:
            payload["safety_reasons"] = list(self.safety_reasons)
        return payload


def _signals_payload(signals: DecisionSignals) -> Mapping[str, Any]:
    return {
        "needs_teacher_review": signals.needs_teacher_review,
        "has_diagnosed_misconception": signals.has_diagnosed_misconception,
        "has_active_misconception": signals.has_active_misconception,
        "has_prerequisite_gap": signals.has_prerequisite_gap,
        "has_new_evidence": signals.has_new_evidence,
        "minimum_recent_correction_rate": float(
            signals.minimum_recent_correction_rate
        ),
        "minimum_mastery_confidence": float(signals.minimum_mastery_confidence),
        "maximum_hint_dependency": float(signals.maximum_hint_dependency),
    }


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-blank string")


def _require_member(value: object, allowed: tuple[str, ...], field_name: str) -> None:
    if value not in allowed:
        raise ValueError(f"{field_name} is not supported")


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a bool")


def _require_nonnegative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_finite(value: object, field_name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


def _require_probability(value: object, field_name: str) -> None:
    _require_finite(value, field_name)
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{field_name} must be a finite probability")


def _require_sha256(value: object, field_name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_relative_artifact_reference(value: object) -> None:
    _require_nonblank(value, "artifact_reference")
    assert isinstance(value, str)
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or windows_path.suffix != ".json"
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError("artifact_reference must be relative and stay below its root")


def _require_unique_strings(values: object, field_name: str) -> None:
    if type(values) is not tuple or any(
        type(value) is not str or not value.strip() for value in values
    ):
        raise ValueError(f"{field_name} must be a tuple of non-blank strings")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


def _require_nonempty_unique_strings(values: object, field_name: str) -> None:
    _require_unique_strings(values, field_name)
    if not values:
        raise ValueError(f"{field_name} must not be empty")


def _normalize_metric_entries(
    values: object,
    field_name: str,
) -> tuple[tuple[str, float], ...]:
    if isinstance(values, Mapping):
        raw_entries = tuple(values.items())
    elif type(values) in {tuple, list}:
        raw_entries = tuple(values)
    else:
        raise ValueError(f"{field_name} must contain named finite metrics")
    normalized: list[tuple[str, float]] = []
    for entry in raw_entries:
        if type(entry) not in {tuple, list} or len(entry) != 2:
            raise ValueError(f"{field_name} must contain named finite metrics")
        name, value = entry
        _require_nonblank(name, field_name)
        _require_finite(value, field_name)
        normalized.append((str(name), float(value)))
    if len({name for name, _ in normalized}) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate names")
    return tuple(sorted(normalized))


def _normalize_confidence_intervals(
    values: object,
    field_name: str,
) -> tuple[tuple[str, float, float], ...]:
    if isinstance(values, Mapping):
        raw_entries = tuple(
            (name, *bounds)
            if type(bounds) in {tuple, list}
            else (name,)
            for name, bounds in values.items()
        )
    elif type(values) in {tuple, list}:
        raw_entries = tuple(values)
    else:
        raise ValueError(f"{field_name} must contain finite interval bounds")
    normalized: list[tuple[str, float, float]] = []
    for entry in raw_entries:
        if type(entry) not in {tuple, list} or len(entry) != 3:
            raise ValueError(f"{field_name} must contain finite interval bounds")
        name, lower, upper = entry
        _require_nonblank(name, field_name)
        _require_finite(lower, field_name)
        _require_finite(upper, field_name)
        if float(lower) > float(upper):
            raise ValueError(f"{field_name} lower bound must not exceed upper bound")
        normalized.append((str(name), float(lower), float(upper)))
    if len({name for name, _, _ in normalized}) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate names")
    return tuple(sorted(normalized))


def _normalize_evaluation_slices(
    values: object,
    field_name: str,
) -> tuple[tuple[str, int, tuple[tuple[str, float], ...]], ...]:
    if type(values) not in {tuple, list}:
        raise ValueError(f"{field_name} must contain immutable slice summaries")
    normalized: list[
        tuple[str, int, tuple[tuple[str, float], ...]]
    ] = []
    for item in values:
        if isinstance(item, Mapping):
            key = item.get("key")
            sample_size = item.get("sample_size")
            metrics = item.get("metrics")
            if set(item) != {"key", "sample_size", "metrics"}:
                raise ValueError(f"{field_name} contains unsupported slice fields")
        elif type(item) in {tuple, list} and len(item) == 3:
            key, sample_size, metrics = item
        else:
            raise ValueError(f"{field_name} contains an invalid slice")
        _require_nonblank(key, field_name)
        if type(sample_size) is not int or sample_size <= 0:
            raise ValueError(f"{field_name} sample_size must be positive")
        normalized_metrics = _normalize_metric_entries(metrics, field_name)
        _require_evaluation_metric_names(
            normalized_metrics,
            field_name,
        )
        if not normalized_metrics:
            raise ValueError(f"{field_name} slice metrics must not be empty")
        normalized.append((str(key), sample_size, normalized_metrics))
    if len({key for key, _, _ in normalized}) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate keys")
    return tuple(sorted(normalized))


def _normalize_string_sequence(
    values: object,
    field_name: str,
) -> tuple[str, ...]:
    if type(values) not in {tuple, list}:
        raise ValueError(f"{field_name} must be a sequence of non-blank strings")
    normalized = tuple(values)
    if any(type(value) is not str or not value.strip() for value in normalized):
        raise ValueError(f"{field_name} must be a sequence of non-blank strings")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(normalized))


def _normalize_audit_identifiers(
    values: object,
    field_name: str,
) -> tuple[str, ...]:
    normalized = _normalize_string_sequence(values, field_name)
    for value in normalized:
        _require_safe_audit_identifier(value, field_name)
    return normalized


def _require_safe_audit_identifier(
    value: object,
    field_name: str,
) -> None:
    if (
        type(value) is not str
        or _SAFE_AUDIT_IDENTIFIER.fullmatch(value) is None
        or _SECRET_LIKE_AUDIT_IDENTIFIER.match(value) is not None
    ):
        raise ValueError(f"{field_name} must be a safe audit identifier")


def _normalize_action_measurements(
    values: object,
    field_name: str,
    *,
    probability: bool,
    allow_empty: bool,
) -> tuple[tuple[str, float], ...]:
    if isinstance(values, Mapping):
        entries = tuple(values.items())
    elif type(values) in {tuple, list}:
        entries = tuple(values)
    else:
        raise ValueError(f"{field_name} must contain action values")
    if not entries and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    normalized: list[tuple[str, float]] = []
    for entry in entries:
        if type(entry) not in {tuple, list} or len(entry) != 2:
            raise ValueError(f"{field_name} must contain action values")
        action_id, value = entry
        _require_nonblank(action_id, field_name)
        if probability:
            _require_probability(value, field_name)
        else:
            _require_finite(value, field_name)
        normalized.append((str(action_id), float(value)))
    if len({action_id for action_id, _ in normalized}) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicate actions")
    return tuple(normalized)


def _require_timezone_string(value: object, field_name: str) -> None:
    _require_nonblank(value, field_name)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")


def _evaluation_slices_payload(
    slices: tuple[tuple[str, int, tuple[tuple[str, float], ...]], ...],
) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "sample_size": sample_size,
            "metrics": dict(metrics),
        }
        for key, sample_size, metrics in slices
    ]


def _require_evaluation_metric_names(
    metrics: tuple[tuple[str, float], ...],
    field_name: str,
) -> None:
    if any(name not in EVALUATION_METRICS for name, _ in metrics):
        raise ValueError(f"{field_name} contains an unsupported metric")
