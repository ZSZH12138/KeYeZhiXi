from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m6_tutoring_fsm.decision_policy import DecisionSignals
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    LoadedPolicyArtifact,
)
from course_insight.modules.m6_tutoring_fsm.policy_gate import (
    ActivePolicyGate,
    PolicyGateConfig,
)
from course_insight.modules.m6_tutoring_fsm.policy_runtime import (
    PolicyRuntime,
    PolicyRuntimeGateInputs,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    PolicyArtifactManifest,
    PolicyDecision,
    PolicyExecutionRef,
    PolicyPrediction,
    TutoringPolicyContext,
)
from course_insight.modules.m6_tutoring_fsm.repository import InMemoryM6Repository


REQUEST_FINGERPRINT = "a" * 64
OTHER_REQUEST_FINGERPRINT = "b" * 64
ARTIFACT_SHA256 = "c" * 64


def _context() -> TutoringPolicyContext:
    return TutoringPolicyContext(
        request_fingerprint=REQUEST_FINGERPRINT,
        current_state="S1",
        task_type="practice",
        turn_count=1,
        score_ratio=0.0,
        target_concept_count=1,
        signals=DecisionSignals(
            needs_teacher_review=False,
            has_diagnosed_misconception=False,
            has_active_misconception=False,
            has_prerequisite_gap=False,
            has_new_evidence=True,
            minimum_recent_correction_rate=0.9,
            minimum_mastery_confidence=0.7,
            maximum_hint_dependency=0.0,
        ),
        learner_evidence_count=1,
    )


def _candidates() -> tuple[CandidateAction, ...]:
    return (
        CandidateAction(
            candidate_id="m6.transition.s1_to_s3.v1",
            next_state="S3",
            action_type="guided_question",
            prompt_template_id="m6.s3.guided_question.v1",
            exploration_allowed=True,
        ),
        CandidateAction(
            candidate_id="m6.transition.s1_to_s2.v1",
            next_state="S2",
            action_type="minimal_hint",
            prompt_template_id="m6.s2.minimal_hint.v1",
            exploration_allowed=True,
        ),
    )


def _manifest() -> PolicyArtifactManifest:
    return PolicyArtifactManifest(
        policy_id="learned-policy-v1",
        adapter_id="test-learned-adapter",
        adapter_version="v1",
        artifact_sha256=ARTIFACT_SHA256,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        gate_policy_version="m6-active-gate-v1",
        status="approved",
        artifact_reference="policy.json",
        allowed_scopes=("course-pseudonymous",),
    )


def _loaded_artifact(
    candidate_ids: tuple[str, ...],
) -> LoadedPolicyArtifact:
    return LoadedPolicyArtifact(
        manifest=_manifest(),
        payload={
            "policy_id": "learned-policy-v1",
            "adapter_id": "test-learned-adapter",
            "adapter_version": "v1",
            "feature_schema_version": "m6-features-v1",
            "action_space_version": "m6-action-space-v1",
            "dimension": 1,
            "alpha": 0.0,
            "actions": {candidate_id: {} for candidate_id in candidate_ids},
        },
    )


class _SelectingAdapter:
    adapter_id = "test-learned-adapter"
    adapter_version = "v1"
    policy_id = "learned-policy-v1"

    def __init__(self, selected_index: int = 1) -> None:
        self._selected_index = selected_index

    def select(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyDecision:
        candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
        selected = candidate_ids[self._selected_index]
        return PolicyDecision(
            request_fingerprint=context.request_fingerprint,
            mode="active",
            selected_candidate_id=selected,
            candidate_ids=candidate_ids,
            prediction=PolicyPrediction(
                policy_id=self.policy_id,
                candidate_id=selected,
                score=0.75,
                propensity=0.8,
            ),
        )


class _FailingAdapter(_SelectingAdapter):
    def select(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyDecision:
        raise ValueError("prediction failed")


class _MismatchedAdapter(_SelectingAdapter):
    adapter_id = "wrong-adapter"


class _InvalidPredictionAdapter(_SelectingAdapter):
    def select(
        self,
        context: TutoringPolicyContext,
        candidates: Sequence[CandidateAction],
    ) -> PolicyDecision:
        decision = super().select(context, candidates)
        assert decision.prediction is not None
        return replace(
            decision,
            prediction=replace(
                decision.prediction,
                policy_id="wrong-policy",
            ),
        )


class _FailingGate:
    def evaluate(self, **kwargs: Any) -> Any:
        raise ValueError("gate failed")


def _gate() -> ActivePolicyGate:
    return ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=10,
            maximum_uncertainty=0.25,
            rollout_percentage=1.0,
            kill_switch=False,
        )
    )


def _gate_inputs() -> PolicyRuntimeGateInputs:
    return PolicyRuntimeGateInputs(
        support=20,
        uncertainty=0.1,
        offline_evaluation_approved=True,
        scope="course-pseudonymous",
    )


def _runtime(
    *,
    mode: str = "active",
    adapter: Any = None,
    artifact_loader: Any = None,
    gate: Any = None,
) -> PolicyRuntime:
    return PolicyRuntime(
        mode=mode,
        learned_adapter=adapter or _SelectingAdapter(),
        artifact_loader=artifact_loader or _loaded_artifact,
        active_gate=gate or _gate(),
        gate_inputs=_gate_inputs(),
    )


def test_rules_mode_does_not_load_optional_policy_artifacts() -> None:
    calls = 0

    def exploding_loader(candidate_ids: tuple[str, ...]) -> LoadedPolicyArtifact:
        nonlocal calls
        calls += 1
        raise AssertionError("rules mode must not load learned artifacts")

    runtime = PolicyRuntime(mode="rules", artifact_loader=exploding_loader)
    context = _context()
    candidates = _candidates()

    execution = runtime.prepare_execution(context, candidates)
    selection = runtime.select(execution, context, candidates)

    assert calls == 0
    assert execution.mode == "rules"
    assert selection.public_candidate.next_state == "S3"
    assert selection.observation.selected_candidate_id == candidates[0].candidate_id
    assert selection.observation.propensity == 1.0


def test_shadow_records_prediction_without_changing_rules_candidate() -> None:
    runtime = _runtime(mode="shadow")
    context = _context()
    candidates = _candidates()

    execution = runtime.prepare_execution(context, candidates)
    selection = runtime.select(execution, context, candidates)

    assert execution.mode == "shadow"
    assert selection.public_candidate.next_state == "S3"
    assert selection.policy_decision.mode == "shadow"
    assert selection.policy_decision.selected_candidate_id == candidates[1].candidate_id
    assert selection.observation.selected_candidate_id == candidates[1].candidate_id
    assert selection.observation.propensity == 0.8


def test_active_adopts_only_a_gate_approved_safe_candidate() -> None:
    runtime = _runtime()
    context = _context()
    candidates = _candidates()

    execution = runtime.prepare_execution(context, candidates)
    selection = runtime.select(execution, context, candidates)

    assert execution.mode == "active"
    assert selection.public_candidate is candidates[1]
    assert selection.policy_decision.selected_candidate_id in {
        candidate.candidate_id for candidate in candidates
    }
    assert selection.policy_decision.fallback_reason is None


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_feature_schema_mismatch_rejects_learned_prediction_provenance(
    mode: str,
) -> None:
    def mismatched_schema_loader(
        candidate_ids: tuple[str, ...],
    ) -> LoadedPolicyArtifact:
        loaded = _loaded_artifact(candidate_ids)
        return replace(
            loaded,
            manifest=replace(
                loaded.manifest,
                feature_schema_version="m6-features-v2",
            ),
        )

    runtime = _runtime(
        mode=mode,
        artifact_loader=mismatched_schema_loader,
    )
    context = _context()
    candidates = _candidates()

    execution = runtime.prepare_execution(context, candidates)
    selection = runtime.select(execution, context, candidates)

    assert execution.feature_schema_version == "m6-features-v2"
    assert selection.public_candidate is candidates[0]
    assert selection.policy_decision.fallback_reason == "policy_runtime_failure"
    assert selection.observation.selected_candidate_id == candidates[0].candidate_id
    assert selection.observation.propensity == 1.0
    assert (
        selection.observation.feature_schema_version
        == execution.feature_schema_version
    )


@pytest.mark.parametrize(
    "runtime",
    [
        _runtime(
            artifact_loader=lambda _: (_ for _ in ()).throw(
                ValueError("artifact")
            )
        ),
        _runtime(adapter=_MismatchedAdapter()),
        _runtime(adapter=_FailingAdapter()),
        _runtime(adapter=_InvalidPredictionAdapter()),
        _runtime(gate=_FailingGate()),
    ],
    ids=("artifact", "adapter", "adapter-call", "prediction", "gate"),
)
def test_active_runtime_failures_fall_back_to_rules(runtime: PolicyRuntime) -> None:
    context = _context()
    candidates = _candidates()

    execution = runtime.prepare_execution(context, candidates)
    selection = runtime.select(execution, context, candidates)

    assert selection.public_candidate.next_state == "S3"
    assert selection.observation.propensity == 1.0
    assert (
        execution.mode == "rules"
        or selection.policy_decision.fallback_reason is not None
    )


def test_policy_execution_fingerprint_is_distinct_and_content_addressed() -> None:
    execution = _runtime().prepare_execution(_context(), _candidates())

    assert execution.policy_execution_fingerprint == execution.identity
    assert len(execution.policy_execution_fingerprint) == 64
    assert execution.policy_execution_fingerprint != REQUEST_FINGERPRINT


def test_repository_keeps_the_immutable_first_policy_writer() -> None:
    repository = InMemoryM6Repository()
    first = PolicyExecutionRef(
        request_fingerprint=REQUEST_FINGERPRINT,
        mode="rules",
        policy_id="m6-deterministic-v1",
        adapter_id="m6-rules-adapter",
        adapter_version="v1",
        artifact_sha256=None,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
    )
    competitor = replace(
        first,
        mode="active",
        policy_id="learned-policy-v1",
        adapter_id="test-learned-adapter",
        artifact_sha256=ARTIFACT_SHA256,
    )

    assert repository.commit_policy_execution(first) == first
    assert repository.commit_policy_execution(competitor) == first
    assert repository.get_policy_execution_by_request(REQUEST_FINGERPRINT) == first


def test_repository_reports_stored_policy_binding_corruption() -> None:
    repository = InMemoryM6Repository()
    corrupt = PolicyExecutionRef(
        request_fingerprint=OTHER_REQUEST_FINGERPRINT,
        mode="rules",
        policy_id="m6-deterministic-v1",
        adapter_id="m6-rules-adapter",
        adapter_version="v1",
        artifact_sha256=None,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
    )
    repository._policy_executions = {REQUEST_FINGERPRINT: corrupt}  # noqa: SLF001

    with pytest.raises(DomainError) as raised:
        repository.get_policy_execution_by_request(REQUEST_FINGERPRINT)

    assert raised.value.code == "TUTORING_POLICY_INTEGRITY_ERROR"
