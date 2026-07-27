"""Domain-level safety and feature invariants for private M6 policy learning."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import math

import pytest

from course_insight.modules.m6_tutoring_fsm.decision_policy import DecisionSignals
from course_insight.modules.m6_tutoring_fsm.features import FeatureBuilder
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    PolicyArtifactManifest,
    PolicyDecision,
    PolicyEvaluationRecord,
    PolicyExecutionRef,
    PolicyPrediction,
    TutoringPolicyContext,
)
from course_insight.modules.m6_tutoring_fsm.safety_envelope import SafetyEnvelope


def _signals(**overrides: object) -> DecisionSignals:
    values: dict[str, object] = {
        "needs_teacher_review": False,
        "has_diagnosed_misconception": False,
        "has_active_misconception": False,
        "has_prerequisite_gap": False,
        "has_new_evidence": True,
        "minimum_recent_correction_rate": 0.9,
        "minimum_mastery_confidence": 0.9,
        "maximum_hint_dependency": 0.0,
    }
    values.update(overrides)
    return DecisionSignals(**values)  # type: ignore[arg-type]


def _context(current_state: str, **signal_overrides: object) -> TutoringPolicyContext:
    return TutoringPolicyContext(
        request_fingerprint="request-001",
        current_state=current_state,
        task_type="practice",
        turn_count=3,
        score_ratio=0.75,
        target_concept_count=2,
        signals=_signals(**signal_overrides),
        learner_evidence_count=4,
        course_id="course-001",
        class_id="class-001",
    )


def _complete_manifest_values() -> dict[str, object]:
    return {
        "policy_id": "policy-001",
        "adapter_id": "m6-linucb-adapter",
        "adapter_version": "v1",
        "algorithm": "linucb",
        "state_graph_version": "m6-state-graph-v1",
        "baseline_policy_version": "m6-policy-v1",
        "feature_schema_version": "m6-features-v1",
        "action_space_version": "m6-action-space-v1",
        "reward_version": "m6-reward-v1",
        "gate_policy_version": "m6-gate-v1",
        "training_data_watermark": "2026-07-27T00:00:00Z",
        "training_data_checksum": "b" * 64,
        "artifact_sha256": "a" * 64,
        "status": "approved",
        "created_at": "2026-07-27T01:00:00Z",
        "artifact_reference": "policy.json",
        "allowed_scopes": ("course:course-001", "class:class-001"),
    }


def test_private_policy_identity_models_freeze_runtime_gate_evidence() -> None:
    context = _context("S1")
    prediction = PolicyPrediction(
        policy_id="policy-001",
        candidate_id="candidate-001",
        score=1.0,
        propensity=1.0,
        uncertainty=0.25,
    )
    execution = PolicyExecutionRef(
        request_fingerprint="a" * 64,
        mode="active",
        policy_id="policy-001",
        adapter_id="m6-linucb-adapter",
        adapter_version="v1",
        artifact_sha256="b" * 64,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        gate_policy_version="m6-gate-v1",
    )

    assert context.course_id == "course-001"
    assert context.class_id == "class-001"
    assert prediction.uncertainty == 0.25
    assert execution.canonical_payload()["gate_policy_version"] == "m6-gate-v1"
    with pytest.raises(ValueError, match="uncertainty"):
        PolicyPrediction(
            policy_id="policy-001",
            candidate_id="candidate-001",
            score=1.0,
            propensity=1.0,
            uncertainty=-0.1,
        )


def test_manifest_requires_the_complete_uploaded_spec() -> None:
    manifest = PolicyArtifactManifest(**_complete_manifest_values())  # type: ignore[arg-type]

    assert set(manifest.canonical_payload()) == {
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
        "training_data_checksum",
        "artifact_sha256",
        "status",
        "created_at",
        "artifact_reference",
        "allowed_scopes",
    }
    incomplete = dict(_complete_manifest_values())
    incomplete.pop("training_data_checksum")
    with pytest.raises(TypeError):
        PolicyArtifactManifest(**incomplete)  # type: ignore[arg-type]


def test_candidate_identity_uses_canonical_json_and_is_immutable() -> None:
    """Catch a non-canonical identity or a mutable policy candidate."""

    candidate = CandidateAction(
        candidate_id="m6.transition.s4_to_s5.v1",
        next_state="S5",
        action_type="summary_and_transfer",
        prompt_template_id="m6.s5.summary.v1",
        exploration_allowed=True,
    )

    assert candidate.canonical_json() == (
        '{"action_type":"summary_and_transfer","candidate_id":'
        '"m6.transition.s4_to_s5.v1","exploration_allowed":true,'
        '"next_state":"S5","prompt_template_id":"m6.s5.summary.v1"}'
    )
    assert candidate.identity == (
        "c9c0d2ead99fe422d7953d5f5b1f2de33c59fee3620cdeedca8c04fdf528e3c8"
    )
    assert not hasattr(candidate, "__dict__")
    with pytest.raises(FrozenInstanceError):
        candidate.next_state = "S3"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("current_state", "signal_overrides", "expected_states", "exploration_allowed"),
    [
        ("S0", {}, ("S1",), True),
        ("S1", {}, ("S3", "S2"), True),
        ("S1", {"needs_teacher_review": True}, ("S2",), False),
        ("S1", {"has_prerequisite_gap": True}, ("S2",), False),
        ("S2", {}, ("S3",), True),
        ("S3", {}, ("S4",), True),
        ("S4", {}, ("S5", "S3", "S2"), True),
        ("S4", {"has_diagnosed_misconception": True}, ("S3", "S2"), True),
        ("S4", {"has_active_misconception": True}, ("S2",), False),
        ("S4", {"has_new_evidence": False}, ("S3", "S2"), True),
        ("S5", {}, (), False),
    ],
)
def test_safety_envelope_returns_the_stable_safe_candidate_matrix(
    current_state: str,
    signal_overrides: dict[str, object],
    expected_states: tuple[str, ...],
    exploration_allowed: bool,
) -> None:
    """Catch illegal, reordered, or explorably unsafe state transitions."""

    candidates = SafetyEnvelope().candidates_for(
        _context(current_state, **signal_overrides)
    )

    assert tuple(candidate.next_state for candidate in candidates) == expected_states
    assert all(
        candidate.exploration_allowed is exploration_allowed for candidate in candidates
    )


def test_policy_context_rejects_invalid_or_nonfinite_structured_values() -> None:
    """Catch invalid policy inputs before they can enter a feature vector."""

    with pytest.raises(ValueError, match="score_ratio"):
        _context("S1").__class__(
            request_fingerprint="request-001",
            current_state="S1",
            task_type="practice",
            turn_count=0,
            score_ratio=math.nan,
            target_concept_count=1,
            signals=_signals(),
            learner_evidence_count=0,
        )
    with pytest.raises(ValueError, match="candidate_id"):
        CandidateAction(" ", "S1", "diagnostic_probe", "m6.s1.diagnostic_probe.v1", True)


@pytest.mark.parametrize(
    "artifact_reference",
    (
        "../policy.json",
        "/policy.json",
        r"C:\\policy.json",
        r"\policy.json",
        r"C:policy.json",
    ),
)
def test_artifact_manifest_rejects_non_relative_artifact_references(
    artifact_reference: str,
) -> None:
    """Catch a manifest reference that could escape the configured artifact root."""

    with pytest.raises(ValueError, match="artifact_reference"):
        PolicyArtifactManifest(
            **(_complete_manifest_values() | {
                "artifact_reference": artifact_reference,
            })
        )


@pytest.mark.parametrize("artifact_reference", ("model.pkl", "weights.joblib"))
def test_artifact_manifest_accepts_only_json_artifact_references(
    artifact_reference: str,
) -> None:
    """Catch an unsafe non-JSON artifact that could trigger deserialization."""

    with pytest.raises(ValueError, match="artifact_reference"):
        PolicyArtifactManifest(
            **(_complete_manifest_values() | {
                "artifact_reference": artifact_reference,
            })
        )


def test_numeric_policy_values_have_one_canonical_representation() -> None:
    """Catch semantically equal integer and float inputs producing different IDs."""

    integer_prediction = PolicyPrediction(
        policy_id="policy-001",
        candidate_id="candidate-001",
        score=1,
        propensity=1,
        uncertainty=0,
    )
    float_prediction = PolicyPrediction(
        policy_id="policy-001",
        candidate_id="candidate-001",
        score=1.0,
        propensity=1.0,
        uncertainty=0.0,
    )

    assert integer_prediction.score == 1.0
    assert integer_prediction.propensity == 1.0
    assert integer_prediction.canonical_json() == float_prediction.canonical_json()
    assert integer_prediction.identity == float_prediction.identity


def test_policy_decision_rejects_prediction_for_an_unselected_candidate() -> None:
    """Catch an impossible decision whose prediction names a different candidate."""

    prediction = PolicyPrediction(
        policy_id="policy-001",
        candidate_id="candidate-b",
        score=0.5,
        propensity=1.0,
        uncertainty=0.0,
    )

    with pytest.raises(ValueError, match="prediction.candidate_id"):
        PolicyDecision(
            request_fingerprint="request-001",
            mode="shadow",
            selected_candidate_id="candidate-a",
            candidate_ids=("candidate-a", "candidate-b"),
            prediction=prediction,
        )


def test_feature_builder_emits_the_fixed_versioned_finite_vector() -> None:
    """Catch reordered, unversioned, or non-finite M6 feature values."""

    vector = FeatureBuilder().build(_context("S4"))

    assert FeatureBuilder.schema_version == "m6-features-v1"
    assert vector == (
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        3.0,
        0.75,
        2.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.9,
        0.9,
        0.0,
        4.0,
    )
    assert all(math.isfinite(value) for value in vector)


def test_policy_evaluation_record_canonically_freezes_complete_ope_evidence() -> None:
    """Catch metrics, intervals, slices, or safety evidence being lost or mutable."""

    record = PolicyEvaluationRecord(
        policy_id="policy-001",
        dataset_identity="d" * 64,
        status="sufficient_data",
        approved=True,
        effective_sample_size=12,
        action_coverage=0.75,
        observation_count=20,
        metrics={"snips": 0.6, "ips": 0.5},
        confidence_intervals={"ips": (0.25, 0.75)},
        state_slices=(
            {
                "key": "S2",
                "sample_size": 4,
                "metrics": {"dr": 0.7, "ips": 0.6},
            },
        ),
        group_slices=(
            {
                "key": "a" * 64,
                "sample_size": 3,
                "metrics": {"dr": 0.65},
            },
        ),
        support_coverage=0.875,
        safety_reasons=(),
    )

    assert record.metrics == (("ips", 0.5), ("snips", 0.6))
    assert record.confidence_intervals == (("ips", 0.25, 0.75),)
    assert record.state_slices == (
        ("S2", 4, (("dr", 0.7), ("ips", 0.6))),
    )
    assert record.canonical_json() == (
        '{"action_coverage":0.75,"approved":true,'
        '"confidence_intervals":{"ips":[0.25,0.75]},'
        '"dataset_identity":"dddddddddddddddddddddddddddddddddddddddddddddddd'
        'dddddddddddddddd","effective_sample_size":12.0,'
        '"group_slices":[{"key":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        'aaaaaaaaaaaaaaaa","metrics":{"dr":0.65},'
        '"sample_size":3}],"metrics":{"ips":0.5,"snips":0.6},'
        '"observation_count":20,"policy_id":"policy-001","safety_reasons":[],'
        '"state_slices":[{"key":"S2","metrics":{"dr":0.7,"ips":0.6},'
        '"sample_size":4}],"status":"sufficient_data",'
        '"support_coverage":0.875}'
    )
    assert not hasattr(record, "__dict__")
    with pytest.raises(FrozenInstanceError):
        record.metrics = ()  # type: ignore[misc]


def test_policy_evaluation_record_requires_observation_count() -> None:
    """Catch active evidence that omits its independently measured support."""

    with pytest.raises(TypeError):
        PolicyEvaluationRecord(
            policy_id="policy-001",
            dataset_identity="dataset-001",
            status="insufficient_data",
            approved=False,
            effective_sample_size=0,
            action_coverage=0,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"metrics": {"ips": math.nan}}, "metrics"),
        (
            {
                "confidence_intervals": {
                    "ips": (0.8, 0.2),
                },
            },
            "confidence_intervals",
        ),
        (
            {
                "state_slices": (
                    {"key": "S2", "sample_size": 1, "metrics": {"dr": 0.5}},
                    {"key": "S2", "sample_size": 2, "metrics": {"dr": 0.6}},
                ),
            },
            "state_slices",
        ),
        ({"support_coverage": math.inf}, "support_coverage"),
        ({"metrics": {"student@example.edu": 0.5}}, "metrics"),
        (
            {
                "state_slices": (
                    {
                        "key": r"C:\Users\Teacher\answer.txt",
                        "sample_size": 1,
                        "metrics": {"dr": 0.5},
                    },
                ),
            },
            "state_slices",
        ),
        (
            {
                "group_slices": (
                    {
                        "key": "student@example.edu",
                        "sample_size": 1,
                        "metrics": {"dr": 0.5},
                    },
                ),
            },
            "group_slices",
        ),
        (
            {"safety_reasons": ("student@example.edu",)},
            "safety_reasons",
        ),
    ],
)
def test_policy_evaluation_record_rejects_invalid_nested_evidence(
    overrides: dict[str, object],
    message: str,
) -> None:
    """Catch non-finite, inverted, or ambiguous OPE evidence."""

    values: dict[str, object] = {
        "policy_id": "policy-001",
        "dataset_identity": "d" * 64,
        "status": "insufficient_data",
        "approved": False,
        "effective_sample_size": 0,
        "action_coverage": 0,
        "observation_count": 0,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        PolicyEvaluationRecord(**values)  # type: ignore[arg-type]
