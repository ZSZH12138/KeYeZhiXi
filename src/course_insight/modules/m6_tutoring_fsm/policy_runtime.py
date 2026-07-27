"""Safe runtime selection across rules, shadow, and active M6 policies."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.modules.m6_tutoring_fsm.features import FeatureBuilder
from course_insight.modules.m6_tutoring_fsm.identity import derive_identifier
from course_insight.modules.m6_tutoring_fsm.policy_adapter import (
    PolicyAdapter,
    RulesPolicyAdapter,
)
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    LoadedPolicyArtifact,
)
from course_insight.modules.m6_tutoring_fsm.policy_gate import ActivePolicyGate
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    POLICY_MODES,
    CandidateAction,
    PolicyDecision,
    PolicyExecutionRef,
    PolicyObservation,
    TutoringPolicyContext,
)


ArtifactLoader = Callable[[tuple[str, ...]], LoadedPolicyArtifact]
ExecutionLoader = Callable[
    [PolicyExecutionRef, tuple[str, ...]],
    tuple[LoadedPolicyArtifact, PolicyAdapter],
]


@dataclass(frozen=True, slots=True)
class PolicyRuntimeGateInputs:
    """Request-independent evidence supplied to the active-policy gate."""

    support: int | None
    offline_evaluation_approved: bool | None
    allowed_course_ids: tuple[str, ...]
    allowed_class_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyRuntimeSelection:
    """The public candidate and private policy evidence for one decision."""

    public_candidate: CandidateAction
    policy_decision: PolicyDecision
    observation: PolicyObservation


class PolicyRuntime:
    """Apply optional learned policies without escaping the safety envelope."""

    def __init__(
        self,
        *,
        mode: str = "rules",
        learned_adapter: PolicyAdapter | None = None,
        artifact_loader: ArtifactLoader | None = None,
        execution_loader: ExecutionLoader | None = None,
        active_gate: ActivePolicyGate | None = None,
        gate_inputs: PolicyRuntimeGateInputs | None = None,
        gate_policy_version: str = "m6-active-gate-v1",
        emergency_kill_switch: bool = False,
    ) -> None:
        if mode not in POLICY_MODES:
            raise ValueError("policy runtime mode is not supported")
        self._mode = mode
        self._rules_adapter = RulesPolicyAdapter()
        self._feature_builder = FeatureBuilder()
        self._learned_adapter = learned_adapter
        self._artifact_loader = artifact_loader
        self._execution_loader = execution_loader
        self._active_gate = active_gate
        self._gate_inputs = gate_inputs
        if not isinstance(gate_policy_version, str) or not gate_policy_version.strip():
            raise ValueError("gate_policy_version must not be blank")
        if type(emergency_kill_switch) is not bool:
            raise ValueError("emergency_kill_switch must be a bool")
        self._gate_policy_version = gate_policy_version
        self._emergency_kill_switch = emergency_kill_switch

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def configured_gate_policy_version(self) -> str:
        """Return the immutable gate version used by rules fallbacks."""

        return self._gate_policy_version

    def prepare_execution(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
        *,
        input_fingerprint: str,
    ) -> PolicyExecutionRef:
        """Build the desired immutable binding, failing safely to rules."""

        ordered = _validated_candidates(candidates)
        if self._mode == "rules":
            return rules_policy_execution(
                context.request_fingerprint,
                gate_policy_version=self._gate_policy_version,
                input_fingerprint=input_fingerprint,
            )
        try:
            self._feature_builder.build(context)
            loaded, adapter = self._load_learned(ordered)
            manifest = loaded.manifest
            exploration_rate = getattr(adapter, "exploration_rate", None)
            if exploration_rate is not None:
                if (
                    type(exploration_rate) not in {int, float}
                    or not 0.0 <= exploration_rate <= 1.0
                ):
                    raise ValueError(
                        "learned adapter exploration rate is invalid"
                    )
                exploration_rate = float(exploration_rate)
            gate_allowed: bool | None = None
            gate_reasons: tuple[str, ...] = ()
            if self._mode == "active":
                preview = adapter.select(context, ordered)
                if (
                    not isinstance(preview, PolicyDecision)
                    or preview.prediction is None
                ):
                    raise ValueError(
                        "active policy preview has no uncertainty"
                    )
                gate = self._evaluate_active_gate(
                    loaded=loaded,
                    context=context,
                    candidate_count=len(ordered),
                    uncertainty=preview.prediction.uncertainty,
                )
                gate_allowed = gate.allowed
                gate_reasons = tuple(gate.reasons)
            return PolicyExecutionRef(
                request_fingerprint=context.request_fingerprint,
                mode=self._mode,
                policy_id=manifest.policy_id,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
                artifact_sha256=manifest.artifact_sha256,
                feature_schema_version=manifest.feature_schema_version,
                action_space_version=manifest.action_space_version,
                gate_policy_version=manifest.gate_policy_version,
                input_fingerprint=input_fingerprint,
                exploration_rate=exploration_rate,
                active_gate_allowed=gate_allowed,
                active_gate_reasons=gate_reasons,
            )
        except Exception:
            return rules_policy_execution(
                context.request_fingerprint,
                gate_policy_version=self._gate_policy_version,
                input_fingerprint=input_fingerprint,
            )

    def select(
        self,
        execution: PolicyExecutionRef,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
        *,
        created_at: str | None = None,
    ) -> PolicyRuntimeSelection:
        """Select one public candidate and produce a de-identified observation."""

        if execution.request_fingerprint != context.request_fingerprint:
            _raise_policy_integrity_error("policy_execution_request_mismatch")
        ordered = _validated_candidates(candidates)
        baseline = self._rules_adapter.select(context, ordered)
        if execution.mode == "rules":
            return _selection(
                execution=execution,
                context=context,
                candidates=ordered,
                baseline_decision=baseline,
                public_decision=baseline,
                observed_decision=baseline,
                decision_source="rules",
                created_at=created_at,
            )
        if execution.mode == "active" and self._emergency_kill_switch:
            return self._fallback(
                execution,
                context,
                ordered,
                baseline,
                "global_kill_switch_enabled",
                created_at=created_at,
            )

        try:
            self._feature_builder.build(context)
            loaded, adapter = self._load_learned(
                ordered,
                execution=execution,
            )
            learned = _validated_learned_decision(
                adapter.select(context, ordered),
                execution,
                context,
                ordered,
            )
            if execution.mode == "shadow":
                return _selection(
                    execution=execution,
                    context=context,
                    candidates=ordered,
                    baseline_decision=baseline,
                    public_decision=baseline,
                    observed_decision=learned,
                    model_decision=learned,
                    decision_source="shadow_baseline",
                    created_at=created_at,
                )
            if execution.active_gate_allowed is None:
                gate = self._evaluate_active_gate(
                    loaded=loaded,
                    context=context,
                    candidate_count=len(ordered),
                    uncertainty=learned.prediction.uncertainty,
                )
                gate_allowed = gate.allowed
                gate_reasons = tuple(gate.reasons)
            else:
                gate_allowed = execution.active_gate_allowed
                gate_reasons = tuple(execution.active_gate_reasons)
            if not gate_allowed:
                return self._fallback(
                    execution,
                    context,
                    ordered,
                    baseline,
                    "active_gate_rejected",
                    created_at=created_at,
                    model_decision=learned,
                    reason_codes=gate_reasons,
                )
            return _selection(
                execution=execution,
                context=context,
                candidates=ordered,
                baseline_decision=baseline,
                public_decision=learned,
                observed_decision=learned,
                model_decision=learned,
                decision_source="active_policy",
                created_at=created_at,
            )
        except Exception:
            return self._fallback(
                execution,
                context,
                ordered,
                baseline,
                "policy_runtime_failure",
                created_at=created_at,
            )

    def _load_learned(
        self,
        candidates: tuple[CandidateAction, ...],
        *,
        execution: PolicyExecutionRef | None = None,
    ) -> tuple[LoadedPolicyArtifact, PolicyAdapter]:
        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        if self._artifact_loader is not None and self._learned_adapter is not None:
            loaded = self._artifact_loader(candidate_ids)
            adapter = self._learned_adapter
            _validate_loaded_components(loaded, adapter)
            if execution is None:
                return loaded, adapter
            try:
                _assert_execution_matches_loaded(execution, loaded, adapter)
            except ValueError:
                pass
            else:
                return loaded, adapter
        if execution is None or self._execution_loader is None:
            raise ValueError("learned runtime components are unavailable")
        loaded, adapter = self._execution_loader(execution, candidate_ids)
        _validate_loaded_components(loaded, adapter)
        _assert_execution_matches_loaded(execution, loaded, adapter)
        return loaded, adapter

    def _evaluate_active_gate(
        self,
        *,
        loaded: LoadedPolicyArtifact,
        context: TutoringPolicyContext,
        candidate_count: int,
        uncertainty: float,
    ) -> Any:
        if self._active_gate is None or self._gate_inputs is None:
            raise ValueError("active gate configuration is unavailable")
        inputs = self._gate_inputs
        manifest = loaded.manifest
        return self._active_gate.evaluate(
            manifest=manifest,
            artifact_sha256=manifest.artifact_sha256,
            feature_schema_version=manifest.feature_schema_version,
            action_space_version=manifest.action_space_version,
            candidate_count=candidate_count,
            support=inputs.support,
            uncertainty=uncertainty,
            offline_evaluation_approved=inputs.offline_evaluation_approved,
            allowed_course_ids=inputs.allowed_course_ids,
            allowed_class_ids=inputs.allowed_class_ids,
            request_fingerprint=context.request_fingerprint,
            context=context,
        )

    @staticmethod
    def _fallback(
        execution: PolicyExecutionRef,
        context: TutoringPolicyContext,
        candidates: tuple[CandidateAction, ...],
        baseline: PolicyDecision,
        reason: str,
        *,
        created_at: str | None,
        model_decision: PolicyDecision | None = None,
        reason_codes: tuple[str, ...] = (),
    ) -> PolicyRuntimeSelection:
        fallback = PolicyDecision(
            request_fingerprint=context.request_fingerprint,
            mode=execution.mode,
            selected_candidate_id=baseline.selected_candidate_id,
            candidate_ids=baseline.candidate_ids,
            prediction=None,
            fallback_reason=reason,
        )
        return _selection(
            execution=execution,
            context=context,
            candidates=candidates,
            baseline_decision=baseline,
            public_decision=fallback,
            observed_decision=fallback,
            model_decision=model_decision,
            decision_source="fallback",
            reason_codes=(reason, *reason_codes),
            created_at=created_at,
        )


def _validate_loaded_components(
    loaded: LoadedPolicyArtifact,
    adapter: PolicyAdapter,
) -> None:
    if not isinstance(loaded, LoadedPolicyArtifact):
        raise ValueError("artifact loader returned an invalid value")
    if (
        adapter.adapter_id != loaded.manifest.adapter_id
        or adapter.adapter_version != loaded.manifest.adapter_version
        or getattr(adapter, "policy_id", None) != loaded.manifest.policy_id
    ):
        raise ValueError("policy adapter does not match its manifest")


def rules_policy_execution(
    request_fingerprint: str,
    *,
    gate_policy_version: str = "m6-active-gate-v1",
    input_fingerprint: str | None = None,
) -> PolicyExecutionRef:
    """Return the canonical deterministic binding for one existing request."""

    adapter = RulesPolicyAdapter()
    return PolicyExecutionRef(
        request_fingerprint=request_fingerprint,
        mode="rules",
        policy_id=adapter.policy_id,
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
        artifact_sha256=None,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        gate_policy_version=gate_policy_version,
        input_fingerprint=input_fingerprint,
    )


def _validated_candidates(
    candidates: Sequence[CandidateAction],
) -> tuple[CandidateAction, ...]:
    ordered = tuple(candidates)
    if not ordered or any(
        not isinstance(candidate, CandidateAction) for candidate in ordered
    ):
        raise ValueError("policy runtime requires safe candidates")
    candidate_ids = tuple(candidate.candidate_id for candidate in ordered)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("policy runtime candidate ids must be unique")
    return ordered


def _validated_learned_decision(
    decision: PolicyDecision,
    execution: PolicyExecutionRef,
    context: TutoringPolicyContext,
    candidates: tuple[CandidateAction, ...],
) -> PolicyDecision:
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    if (
        not isinstance(decision, PolicyDecision)
        or decision.request_fingerprint != context.request_fingerprint
        or decision.candidate_ids != candidate_ids
        or decision.prediction is None
        or decision.prediction.policy_id != execution.policy_id
        or decision.prediction.feature_schema_version
        != execution.feature_schema_version
        or decision.prediction.action_probabilities is None
        or decision.prediction.model_scores is None
        or {
            action_id
            for action_id, _ in decision.prediction.action_probabilities
        }
        != set(candidate_ids)
        or {
            action_id
            for action_id, _ in decision.prediction.model_scores
        }
        != set(candidate_ids)
    ):
        raise ValueError("learned policy returned an invalid prediction")
    return PolicyDecision(
        request_fingerprint=context.request_fingerprint,
        mode=execution.mode,
        selected_candidate_id=decision.selected_candidate_id,
        candidate_ids=candidate_ids,
        prediction=decision.prediction,
    )


def _assert_execution_matches_loaded(
    execution: PolicyExecutionRef,
    loaded: LoadedPolicyArtifact,
    adapter: PolicyAdapter,
) -> None:
    manifest = loaded.manifest
    if (
        execution.policy_id != manifest.policy_id
        or execution.adapter_id != adapter.adapter_id
        or execution.adapter_version != adapter.adapter_version
        or execution.artifact_sha256 != manifest.artifact_sha256
        or execution.feature_schema_version != manifest.feature_schema_version
        or execution.action_space_version != manifest.action_space_version
        or execution.gate_policy_version != manifest.gate_policy_version
        or (
            execution.exploration_rate is not None
            and execution.exploration_rate
            != getattr(adapter, "exploration_rate", None)
        )
    ):
        raise ValueError("prepared policy execution no longer matches runtime")


def _selection(
    *,
    execution: PolicyExecutionRef,
    context: TutoringPolicyContext,
    candidates: tuple[CandidateAction, ...],
    baseline_decision: PolicyDecision,
    public_decision: PolicyDecision,
    observed_decision: PolicyDecision,
    model_decision: PolicyDecision | None = None,
    decision_source: str,
    reason_codes: tuple[str, ...] = (),
    created_at: str | None,
) -> PolicyRuntimeSelection:
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    public_candidate = by_id.get(public_decision.selected_candidate_id)
    if public_candidate is None:
        raise ValueError("policy selected a candidate outside the safety envelope")
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    logging_prediction = (
        public_decision.prediction
        if decision_source == "active_policy"
        else None
    )
    if logging_prediction is None:
        logging_probabilities = tuple(
            (
                candidate_id,
                1.0
                if candidate_id == public_decision.selected_candidate_id
                else 0.0,
            )
            for candidate_id in candidate_ids
        )
    else:
        logging_probabilities = tuple(logging_prediction.action_probabilities)
    propensity = dict(logging_probabilities)[public_decision.selected_candidate_id]
    if execution.input_fingerprint is None:
        observation = PolicyObservation(
            policy_execution_fingerprint=execution.policy_execution_fingerprint,
            request_fingerprint=execution.request_fingerprint,
            feature_schema_version=execution.feature_schema_version,
            candidate_ids=candidate_ids,
            selected_candidate_id=public_decision.selected_candidate_id,
            propensity=propensity,
        )
    else:
        if created_at is None:
            raise ValueError("new policy observations require created_at")
        audit_prediction = (
            None if model_decision is None else model_decision.prediction
        )
        model_scores = (
            ()
            if audit_prediction is None
            else tuple(audit_prediction.model_scores)
        )
        observation = PolicyObservation(
            policy_execution_fingerprint=execution.policy_execution_fingerprint,
            request_fingerprint=execution.request_fingerprint,
            feature_schema_version=execution.feature_schema_version,
            candidate_ids=candidate_ids,
            selected_candidate_id=public_decision.selected_candidate_id,
            propensity=propensity,
            decision_id=derive_identifier(
                "decision",
                execution.input_fingerprint,
            ),
            input_fingerprint=execution.input_fingerprint,
            context_checksum=context.identity,
            candidate_set_checksum=_candidate_set_checksum(candidates),
            baseline_action_id=baseline_decision.selected_candidate_id,
            chosen_action_id=public_decision.selected_candidate_id,
            action_probabilities=logging_probabilities,
            model_scores=model_scores,
            uncertainty=(
                None if audit_prediction is None else audit_prediction.uncertainty
            ),
            decision_source=decision_source,
            shadow_action_id=(
                model_decision.selected_candidate_id
                if decision_source == "shadow_baseline"
                and model_decision is not None
                else None
            ),
            reason_codes=reason_codes,
            policy_id=execution.policy_id,
            adapter_id=execution.adapter_id,
            adapter_version=execution.adapter_version,
            artifact_sha256=execution.artifact_sha256,
            action_space_version=execution.action_space_version,
            gate_policy_version=execution.gate_policy_version,
            logging_policy_id=(
                execution.policy_id
                if decision_source == "active_policy"
                else RulesPolicyAdapter().policy_id
            ),
            created_at=created_at,
        )
    return PolicyRuntimeSelection(
        public_candidate=public_candidate,
        policy_decision=observed_decision,
        observation=observation,
    )


def _candidate_set_checksum(
    candidates: tuple[CandidateAction, ...],
) -> str:
    payload = json.dumps(
        [candidate.canonical_payload() for candidate in candidates],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _raise_policy_integrity_error(reason: str) -> None:
    raise DomainError(
        code="TUTORING_POLICY_INTEGRITY_ERROR",
        module="m6",
        message="stored tutoring policy execution is inconsistent",
        details={"reason": reason},
    )
