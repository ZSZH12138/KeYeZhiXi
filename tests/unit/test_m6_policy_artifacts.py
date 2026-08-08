from __future__ import annotations

from hashlib import sha256
import json

import pytest

from course_insight.modules.m6_tutoring_fsm.decision_policy import DecisionSignals
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    load_policy_artifact,
    load_policy_artifact_for_manifest,
)
from course_insight.modules.m6_tutoring_fsm.policy_gate import ActivePolicyGate, PolicyGateConfig
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
    TutoringPolicyContext,
)


EXPECTED_ACTION_IDS = (
    "m6.transition.s1_to_s3.v1",
    "m6.transition.s1_to_s2.v1",
)


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _context(**signal_overrides: bool) -> TutoringPolicyContext:
    signals = DecisionSignals(
        needs_teacher_review=False,
        has_diagnosed_misconception=False,
        has_active_misconception=False,
        has_prerequisite_gap=False,
        has_new_evidence=True,
        minimum_recent_correction_rate=1.0,
        minimum_mastery_confidence=1.0,
        maximum_hint_dependency=0.0,
    )
    if signal_overrides:
        signals = DecisionSignals(
            **{name: getattr(signals, name) for name in signals.__dataclass_fields__}
            | signal_overrides
        )
    return TutoringPolicyContext(
        request_fingerprint="request-1",
        current_state="S1",
        task_type="practice",
        turn_count=1,
        score_ratio=0.5,
        target_concept_count=1,
        signals=signals,
        learner_evidence_count=1,
        course_id="course-1",
        class_id="class-1",
    )


def _write_bundle(tmp_path, *, digest: str | None = None, artifact_overrides: dict[str, object] | None = None):
    artifact = {
        "action_space_version": "m6-action-space-v1",
        "actions": {
            "m6.transition.s1_to_s3.v1": {
                "inverse_covariance": [[1.0, 0.0], [0.0, 1.0]],
                "theta": [0.0, 1.0],
            },
            "m6.transition.s1_to_s2.v1": {
                "inverse_covariance": [[1.0, 0.0], [0.0, 1.0]],
                "theta": [0.0, 0.0],
            },
        },
        "adapter_id": "m6-linucb-adapter",
        "adapter_version": "v1",
        "alpha": 0.1,
        "dimension": 2,
        "feature_schema_version": "m6-features-v1",
        "policy_id": "m6-linucb-v1",
    } | (artifact_overrides or {})
    artifact_json = _canonical(artifact)
    (tmp_path / "model.json").write_text(artifact_json, encoding="utf-8")
    manifest = {
        "action_space_version": "m6-action-space-v1",
        "adapter_id": "m6-linucb-adapter",
        "adapter_version": "v1",
        "algorithm": "linucb",
        "baseline_policy_version": "m6-policy-v1",
        "created_at": "2026-07-27T01:00:00Z",
        "allowed_scopes": ["course:course-1", "class:class-1"],
        "artifact_reference": "model.json",
        "artifact_sha256": digest or sha256(artifact_json.encode("utf-8")).hexdigest(),
        "feature_schema_version": "m6-features-v1",
        "gate_policy_version": "m6-active-gate-v1",
        "policy_id": "m6-linucb-v1",
        "reward_version": "m6-reward-v1",
        "state_graph_version": "m6-state-graph-v1",
        "status": "approved",
        "training_data_checksum": "b" * 64,
        "training_data_watermark": "2026-07-27T00:00:00Z",
    }
    (tmp_path / "manifest.json").write_text(_canonical(manifest), encoding="utf-8")
    return manifest, artifact


def test_loads_only_canonical_json_and_checks_digest_version_dimension_and_actions(tmp_path) -> None:
    manifest, artifact = _write_bundle(tmp_path)

    loaded = load_policy_artifact(
        tmp_path, "manifest.json", expected_action_ids=EXPECTED_ACTION_IDS
    )

    assert loaded.manifest == PolicyArtifactManifest(
        **(
            manifest
            | {"allowed_scopes": ("course:course-1", "class:class-1")}
        )
    )
    assert loaded.payload == artifact

    _write_bundle(tmp_path, digest="0" * 64)
    with pytest.raises(ValueError, match="digest"):
        load_policy_artifact(
            tmp_path, "manifest.json", expected_action_ids=EXPECTED_ACTION_IDS
        )


def test_repository_manifest_loader_accepts_a_safe_request_subset(
    tmp_path,
) -> None:
    manifest_data, artifact = _write_bundle(
        tmp_path,
        artifact_overrides={
            "actions": {
                **_write_bundle(tmp_path)[1]["actions"],
                "m6.transition.s4_to_s5.v1": {
                    "inverse_covariance": [[1.0, 0.0], [0.0, 1.0]],
                    "theta": [1.0, 0.0],
                },
            }
        },
    )
    manifest = PolicyArtifactManifest(
        **(
            manifest_data
            | {"allowed_scopes": ("course:course-1", "class:class-1")}
        )
    )

    loaded = load_policy_artifact_for_manifest(
        tmp_path,
        manifest,
        expected_action_ids=("m6.transition.s1_to_s3.v1",),
    )

    assert set(loaded.payload["actions"]) > {
        "m6.transition.s1_to_s3.v1",
    }

    _write_bundle(tmp_path, artifact_overrides={"dimension": 3})
    with pytest.raises(ValueError, match="dimension"):
        load_policy_artifact(
            tmp_path, "manifest.json", expected_action_ids=EXPECTED_ACTION_IDS
        )


@pytest.mark.parametrize("reference", ["../manifest.json", "..\\manifest.json", "C:\\manifest.json", "/manifest.json"])
def test_artifact_loader_rejects_windows_and_posix_path_escapes(tmp_path, reference: str) -> None:
    _write_bundle(tmp_path)

    with pytest.raises(ValueError, match="relative"):
        load_policy_artifact(tmp_path, reference, expected_action_ids=EXPECTED_ACTION_IDS)


def test_artifact_loader_rejects_noncanonical_json_and_manifest_artifact_mismatch(tmp_path) -> None:
    _write_bundle(tmp_path)
    (tmp_path / "manifest.json").write_text('{"z": 1, "a": 2}', encoding="utf-8")

    with pytest.raises(ValueError, match="canonical"):
        load_policy_artifact(
            tmp_path, "manifest.json", expected_action_ids=EXPECTED_ACTION_IDS
        )

    _write_bundle(tmp_path, artifact_overrides={"adapter_version": "v2"})
    with pytest.raises(ValueError, match="adapter_version"):
        load_policy_artifact(
            tmp_path, "manifest.json", expected_action_ids=EXPECTED_ACTION_IDS
        )


def test_artifact_loader_rejects_an_expected_candidate_action_mismatch(tmp_path) -> None:
    _write_bundle(tmp_path)

    with pytest.raises(ValueError, match="action"):
        load_policy_artifact(
            tmp_path,
            "manifest.json",
            expected_action_ids=("unknown-safe-action",),
        )


def test_artifact_loader_requires_a_nonempty_current_safe_action_set(tmp_path) -> None:
    _write_bundle(tmp_path)

    with pytest.raises(ValueError, match="expected action"):
        load_policy_artifact(tmp_path, "manifest.json", expected_action_ids=None)


def test_artifact_loader_normalizes_invalid_manifest_errors_and_requires_manifest_type(
    tmp_path,
) -> None:
    _write_bundle(tmp_path)
    (tmp_path / "manifest.json").write_text(
        _canonical({"allowed_scopes": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid policy manifest"):
        load_policy_artifact(
            tmp_path,
            "manifest.json",
            expected_action_ids=EXPECTED_ACTION_IDS,
        )

    _write_bundle(tmp_path)
    with pytest.raises(TypeError, match="PolicyArtifactManifest"):
        load_policy_artifact_for_manifest(
            tmp_path,
            object(),  # type: ignore[arg-type]
            expected_action_ids=EXPECTED_ACTION_IDS,
        )


@pytest.mark.parametrize("reference", [None, "", "   "])
def test_artifact_loader_rejects_blank_or_non_string_references(
    tmp_path, reference: object
) -> None:
    _write_bundle(tmp_path)

    with pytest.raises(ValueError, match="relative path"):
        load_policy_artifact(
            tmp_path,
            reference,  # type: ignore[arg-type]
            expected_action_ids=EXPECTED_ACTION_IDS,
        )


def test_artifact_loader_rejects_non_json_references(tmp_path) -> None:
    manifest, _ = _write_bundle(tmp_path)
    (tmp_path / "manifest.txt").write_text(_canonical(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="must be JSON"):
        load_policy_artifact(
            tmp_path,
            "manifest.txt",
            expected_action_ids=EXPECTED_ACTION_IDS,
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff", "unable to load JSON artifact"),
        (b"{", "invalid JSON artifact"),
        (b"[]", "root must be an object"),
        (b'{"key":1,"key":2}', "invalid JSON artifact"),
        (b'{"value":NaN}', "invalid JSON artifact"),
    ],
)
def test_artifact_loader_rejects_unsafe_json_encodings_and_shapes(
    tmp_path, raw: bytes, message: str
) -> None:
    _write_bundle(tmp_path)
    (tmp_path / "manifest.json").write_bytes(raw)

    with pytest.raises(ValueError, match=message):
        load_policy_artifact(
            tmp_path,
            "manifest.json",
            expected_action_ids=EXPECTED_ACTION_IDS,
        )


@pytest.mark.parametrize(
    ("invalid_case", "message"),
    [
        ("schema", "invalid schema"),
        ("dimension", "positive integer"),
        ("alpha", "finite and non-negative"),
        ("actions", "non-empty object"),
        ("action_id", "action is invalid"),
        ("action_parameters", "action is invalid"),
        ("parameter_schema", "parameters are invalid"),
        ("matrix_dimension", "inverse covariance has invalid dimension"),
        ("vector_number", "theta must be finite"),
    ],
)
def test_artifact_loader_rejects_malformed_model_parameters(
    tmp_path, invalid_case: str, message: str
) -> None:
    _, artifact = _write_bundle(tmp_path)
    actions = artifact["actions"]
    assert isinstance(actions, dict)
    first_parameters = actions[EXPECTED_ACTION_IDS[0]]
    assert isinstance(first_parameters, dict)

    if invalid_case == "schema":
        artifact["unexpected"] = True
    elif invalid_case == "dimension":
        artifact["dimension"] = 0
    elif invalid_case == "alpha":
        artifact["alpha"] = -0.1
    elif invalid_case == "actions":
        artifact["actions"] = {}
    elif invalid_case == "action_id":
        artifact["actions"] = {"": first_parameters}
    elif invalid_case == "action_parameters":
        actions[EXPECTED_ACTION_IDS[0]] = []
    elif invalid_case == "parameter_schema":
        actions[EXPECTED_ACTION_IDS[0]] = {"theta": [0.0, 1.0]}
    elif invalid_case == "matrix_dimension":
        first_parameters["inverse_covariance"] = [[1.0, 0.0]]
    else:
        first_parameters["theta"] = [0.0, "not-a-number"]

    _write_bundle(tmp_path, artifact_overrides=artifact)

    with pytest.raises(ValueError, match=message):
        load_policy_artifact(
            tmp_path,
            "manifest.json",
            expected_action_ids=EXPECTED_ACTION_IDS,
        )


def test_active_gate_requires_every_approved_condition_and_reports_all_reasons(tmp_path) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(manifest_data | {
            "allowed_scopes": ("course:course-1", "class:class-1")
        })
    )
    gate = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=10,
            maximum_uncertainty=0.2,
            rollout_percentage=0.0,
            kill_switch=False,
        )
    )

    result = gate.evaluate(
        manifest=manifest,
        artifact_sha256="f" * 64,
        feature_schema_version="wrong",
        action_space_version="wrong",
        candidate_count=1,
        support=9,
        uncertainty=0.3,
        offline_evaluation_approved=False,
        allowed_course_ids=("course-2",),
        allowed_class_ids=("class-2",),
        request_fingerprint="request-1",
        context=_context(),
    )

    assert result.allowed is False
    assert result.reasons == (
        "artifact_sha256_mismatch",
        "feature_schema_version_mismatch",
        "action_space_version_mismatch",
        "candidate_count_too_low",
        "support_too_low",
        "uncertainty_too_high",
        "offline_evaluation_not_approved",
        "scope_not_allowed",
        "rollout_not_selected",
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"gate_policy_version": "   "}, "must not be blank"),
        ({"minimum_support": True}, "non-negative integer"),
        ({"maximum_uncertainty": float("nan")}, "finite and non-negative"),
        ({"rollout_percentage": 1.1}, "must be a probability"),
        ({"kill_switch": 0}, "must be a bool"),
    ],
)
def test_active_gate_rejects_invalid_configuration(
    overrides: dict[str, object], message: str
) -> None:
    values = {
        "gate_policy_version": "m6-active-gate-v1",
        "minimum_support": 0,
        "maximum_uncertainty": 1.0,
        "rollout_percentage": 1.0,
        "kill_switch": False,
    } | overrides

    with pytest.raises(ValueError, match=message):
        PolicyGateConfig(**values)  # type: ignore[arg-type]


def test_active_gate_requires_a_valid_config_instance() -> None:
    with pytest.raises(ValueError, match="PolicyGateConfig"):
        ActivePolicyGate(object())  # type: ignore[arg-type]


def test_active_gate_reports_manifest_and_context_identity_mismatches(tmp_path) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(
            manifest_data
            | {
                "allowed_scopes": ("course:course-1", "class:class-1"),
                "status": "retired",
                "gate_policy_version": "m6-retired-gate-v1",
            }
        )
    )
    result = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=0,
            maximum_uncertainty=1.0,
            rollout_percentage=1.0,
            kill_switch=False,
        )
    ).evaluate(
        manifest=manifest,
        artifact_sha256=manifest.artifact_sha256,
        feature_schema_version=manifest.feature_schema_version,
        action_space_version=manifest.action_space_version,
        candidate_count=2,
        support=0,
        uncertainty=0.0,
        offline_evaluation_approved=True,
        allowed_course_ids=("course-1",),
        allowed_class_ids=("class-1",),
        request_fingerprint="request-2",
        context=_context(),
    )

    assert result.reasons == (
        "manifest_not_approved",
        "gate_policy_version_mismatch",
        "policy_context_mismatch",
    )


def test_active_gate_fails_closed_for_a_structurally_invalid_context(tmp_path) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(
            manifest_data
            | {"allowed_scopes": ("course:course-1", "class:class-1")}
        )
    )

    class InvalidContext:
        course_id = "course-1"
        class_id = "class-1"

    result = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=0,
            maximum_uncertainty=1.0,
            rollout_percentage=1.0,
            kill_switch=False,
        )
    ).evaluate(
        manifest=manifest,
        artifact_sha256=manifest.artifact_sha256,
        feature_schema_version=manifest.feature_schema_version,
        action_space_version=manifest.action_space_version,
        candidate_count=2,
        support=0,
        uncertainty=0.0,
        offline_evaluation_approved=True,
        allowed_course_ids=("course-1",),
        allowed_class_ids=("class-1",),
        request_fingerprint="request-1",
        context=InvalidContext(),  # type: ignore[arg-type]
    )

    assert result.reasons == ("policy_context_invalid",)


def test_active_gate_uses_a_deterministic_partial_rollout_bucket(tmp_path) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(
            manifest_data
            | {"allowed_scopes": ("course:course-1", "class:class-1")}
        )
    )
    result = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=0,
            maximum_uncertainty=1.0,
            rollout_percentage=0.5,
            kill_switch=False,
        )
    ).evaluate(
        manifest=manifest,
        artifact_sha256=manifest.artifact_sha256,
        feature_schema_version=manifest.feature_schema_version,
        action_space_version=manifest.action_space_version,
        candidate_count=2,
        support=0,
        uncertainty=0.0,
        offline_evaluation_approved=True,
        allowed_course_ids=("course-1",),
        allowed_class_ids=("class-1",),
        request_fingerprint="request-1",
        context=_context(),
    )

    assert result.allowed is True


def test_active_gate_allows_valid_rollout_and_fails_closed_for_kill_switch_or_missing_manifest(tmp_path) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(manifest_data | {
            "allowed_scopes": ("course:course-1", "class:class-1")
        })
    )
    config = PolicyGateConfig(
        gate_policy_version="m6-active-gate-v1",
        minimum_support=10,
        maximum_uncertainty=0.2,
        rollout_percentage=1.0,
        kill_switch=False,
    )
    common = {
        "manifest": manifest,
        "artifact_sha256": manifest.artifact_sha256,
        "feature_schema_version": manifest.feature_schema_version,
        "action_space_version": manifest.action_space_version,
        "candidate_count": 2,
        "support": 10,
        "uncertainty": 0.2,
        "offline_evaluation_approved": True,
        "allowed_course_ids": ("course-1",),
        "allowed_class_ids": ("class-1",),
        "request_fingerprint": "request-1",
        "context": _context(),
    }

    assert ActivePolicyGate(config).evaluate(**common).allowed is True
    assert ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=10,
            maximum_uncertainty=0.2,
            rollout_percentage=1.0,
            kill_switch=True,
        )
    ).evaluate(**common).reasons == ("kill_switch_enabled",)
    assert ActivePolicyGate(config).evaluate(**(common | {"manifest": None})).reasons == ("manifest_missing",)


def test_active_gate_reports_kill_switch_and_rollout_rejections_together(tmp_path) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(manifest_data | {
            "allowed_scopes": ("course:course-1", "class:class-1")
        })
    )
    result = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=0,
            maximum_uncertainty=1.0,
            rollout_percentage=0.0,
            kill_switch=True,
        )
    ).evaluate(
        manifest=manifest,
        artifact_sha256=manifest.artifact_sha256,
        feature_schema_version=manifest.feature_schema_version,
        action_space_version=manifest.action_space_version,
        candidate_count=2,
        support=0,
        uncertainty=0.0,
        offline_evaluation_approved=True,
        allowed_course_ids=("course-1",),
        allowed_class_ids=("class-1",),
        request_fingerprint="request-1",
        context=_context(),
    )

    assert result.reasons == ("kill_switch_enabled", "rollout_not_selected")


@pytest.mark.parametrize(
    "signal_overrides",
    [
        {"needs_teacher_review": True},
        {"has_diagnosed_misconception": True},
        {"has_active_misconception": True},
        {"has_prerequisite_gap": True},
    ],
)
def test_active_gate_rejects_every_remediation_signal(
    tmp_path, signal_overrides: dict[str, bool]
) -> None:
    manifest_data, _ = _write_bundle(tmp_path)
    manifest = PolicyArtifactManifest(
        **(manifest_data | {
            "allowed_scopes": ("course:course-1", "class:class-1")
        })
    )

    result = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version="m6-active-gate-v1",
            minimum_support=0,
            maximum_uncertainty=1.0,
            rollout_percentage=1.0,
            kill_switch=False,
        )
    ).evaluate(
        manifest=manifest,
        artifact_sha256=manifest.artifact_sha256,
        feature_schema_version=manifest.feature_schema_version,
        action_space_version=manifest.action_space_version,
        candidate_count=2,
        support=0,
        uncertainty=0.0,
        offline_evaluation_approved=True,
        allowed_course_ids=("course-1",),
        allowed_class_ids=("class-1",),
        request_fingerprint="request-1",
        context=_context(**signal_overrides),
    )

    assert result.allowed is False
    assert result.reasons == ("remediation_required",)
