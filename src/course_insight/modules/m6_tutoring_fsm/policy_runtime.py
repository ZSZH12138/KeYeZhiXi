"""Safe runtime selection across rules, shadow, and active M6 policies."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.modules.m6_tutoring_fsm.features import FeatureBuilder
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
        active_gate: ActivePolicyGate | None = None,
        gate_inputs: PolicyRuntimeGateInputs | None = None,
        gate_policy_version: str = "m6-active-gate-v1",
    ) -> None:
        if mode not in POLICY_MODES:
            raise ValueError("policy runtime mode is not supported")
        self._mode = mode
        self._rules_adapter = RulesPolicyAdapter()
        self._feature_builder = FeatureBuilder()
        self._learned_adapter = learned_adapter
        self._artifact_loader = artifact_loader
        self._active_gate = active_gate
        self._gate_inputs = gate_inputs
        if not isinstance(gate_policy_version, str) or not gate_policy_version.strip():
            raise ValueError("gate_policy_version must not be blank")
        self._gate_policy_version = gate_policy_version

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
    ) -> PolicyExecutionRef:
        """Build the desired immutable binding, failing safely to rules."""

        ordered = _validated_candidates(candidates)
        if self._mode == "rules":
            return rules_policy_execution(
                context.request_fingerprint,
                gate_policy_version=self._gate_policy_version,
            )
        try:
            self._feature_builder.build(context)
            loaded, adapter = self._load_learned(ordered)
            manifest = loaded.manifest
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
            )
        except Exception:
            return rules_policy_execution(
                context.request_fingerprint,
                gate_policy_version=self._gate_policy_version,
            )

    def select(
        self,
        execution: PolicyExecutionRef,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyRuntimeSelection:
        """Select one public candidate and produce a de-identified observation."""

        if execution.request_fingerprint != context.request_fingerprint:
            _raise_policy_integrity_error("policy_execution_request_mismatch")
        ordered = _validated_candidates(candidates)
        baseline = self._rules_adapter.select(context, ordered)
        if execution.mode == "rules":
            return _selection(
                execution=execution,
                candidates=ordered,
                public_decision=baseline,
                observed_decision=baseline,
            )

        try:
            self._feature_builder.build(context)
            loaded, adapter = self._load_learned(ordered)
            _assert_execution_matches_loaded(execution, loaded, adapter)
            learned = _validated_learned_decision(
                adapter.select(context, ordered),
                execution,
                context,
                ordered,
            )
            if execution.mode == "shadow":
                return _selection(
                    execution=execution,
                    candidates=ordered,
                    public_decision=baseline,
                    observed_decision=learned,
                )
            gate = self._evaluate_active_gate(
                loaded=loaded,
                context=context,
                candidate_count=len(ordered),
                uncertainty=learned.prediction.uncertainty,
            )
            if not gate.allowed:
                return self._fallback(
                    execution,
                    context,
                    ordered,
                    baseline,
                    "active_gate_rejected",
                )
            return _selection(
                execution=execution,
                candidates=ordered,
                public_decision=learned,
                observed_decision=learned,
            )
        except Exception:
            return self._fallback(
                execution,
                context,
                ordered,
                baseline,
                "policy_runtime_failure",
            )

    def _load_learned(
        self,
        candidates: tuple[CandidateAction, ...],
    ) -> tuple[LoadedPolicyArtifact, PolicyAdapter]:
        if self._artifact_loader is None or self._learned_adapter is None:
            raise ValueError("learned runtime components are unavailable")
        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        loaded = self._artifact_loader(candidate_ids)
        if not isinstance(loaded, LoadedPolicyArtifact):
            raise ValueError("artifact loader returned an invalid value")
        adapter = self._learned_adapter
        if (
            adapter.adapter_id != loaded.manifest.adapter_id
            or adapter.adapter_version != loaded.manifest.adapter_version
            or getattr(adapter, "policy_id", None) != loaded.manifest.policy_id
        ):
            raise ValueError("policy adapter does not match its manifest")
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
            candidates=candidates,
            public_decision=fallback,
            observed_decision=fallback,
        )


def rules_policy_execution(
    request_fingerprint: str,
    *,
    gate_policy_version: str = "m6-active-gate-v1",
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
    ):
        raise ValueError("prepared policy execution no longer matches runtime")


def _selection(
    *,
    execution: PolicyExecutionRef,
    candidates: tuple[CandidateAction, ...],
    public_decision: PolicyDecision,
    observed_decision: PolicyDecision,
) -> PolicyRuntimeSelection:
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    public_candidate = by_id.get(public_decision.selected_candidate_id)
    if public_candidate is None:
        raise ValueError("policy selected a candidate outside the safety envelope")
    prediction = observed_decision.prediction
    propensity = 1.0 if prediction is None else prediction.propensity
    observation = PolicyObservation(
        policy_execution_fingerprint=execution.policy_execution_fingerprint,
        request_fingerprint=execution.request_fingerprint,
        feature_schema_version=execution.feature_schema_version,
        candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
        selected_candidate_id=observed_decision.selected_candidate_id,
        propensity=propensity,
    )
    return PolicyRuntimeSelection(
        public_candidate=public_candidate,
        policy_decision=observed_decision,
        observation=observation,
    )


def _raise_policy_integrity_error(reason: str) -> None:
    raise DomainError(
        code="TUTORING_POLICY_INTEGRITY_ERROR",
        module="m6",
        message="stored tutoring policy execution is inconsistent",
        details={"reason": reason},
    )
