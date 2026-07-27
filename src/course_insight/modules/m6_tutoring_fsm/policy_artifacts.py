"""Canonical JSON-only M6 policy artifact loading under one runtime root."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from course_insight.modules.m6_tutoring_fsm.policy_types import PolicyArtifactManifest


@dataclass(frozen=True, slots=True)
class LoadedPolicyArtifact:
    """A manifest and verified, normalized artifact payload."""

    manifest: PolicyArtifactManifest
    payload: Mapping[str, Any]


def load_policy_artifact(
    root: str | Path,
    manifest_reference: str,
    *,
    expected_action_ids: tuple[str, ...],
) -> LoadedPolicyArtifact:
    """Load a canonical manifest and its canonical JSON artifact below ``root``."""

    root_path = Path(root).resolve(strict=True)
    manifest_path = _resolve_under_root(root_path, manifest_reference)
    manifest_data = _load_canonical_json(manifest_path)
    try:
        manifest = PolicyArtifactManifest(
            **(manifest_data | {"allowed_scopes": tuple(manifest_data.get("allowed_scopes", ()))})
        )
    except (TypeError, ValueError) as error:
        raise ValueError("invalid policy manifest") from error
    return load_policy_artifact_for_manifest(
        root_path,
        manifest,
        expected_action_ids=expected_action_ids,
    )


def load_policy_artifact_for_manifest(
    root: str | Path,
    manifest: PolicyArtifactManifest,
    *,
    expected_action_ids: tuple[str, ...],
) -> LoadedPolicyArtifact:
    """Load an artifact for a repository-selected immutable manifest."""

    if not isinstance(manifest, PolicyArtifactManifest):
        raise TypeError("manifest must be a PolicyArtifactManifest")
    root_path = Path(root).resolve(strict=True)
    artifact_path = _resolve_under_root(root_path, manifest.artifact_reference)
    artifact_data, artifact_bytes = _load_canonical_json_with_bytes(artifact_path)
    actual_digest = sha256(artifact_bytes).hexdigest()
    if actual_digest != manifest.artifact_sha256:
        raise ValueError("artifact digest does not match manifest")
    _validate_artifact_matches_manifest(artifact_data, manifest)
    _validate_expected_actions(artifact_data["actions"], expected_action_ids)
    return LoadedPolicyArtifact(manifest=manifest, payload=artifact_data)


def _resolve_under_root(root: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("artifact reference must be a relative path")
    windows = PureWindowsPath(reference)
    posix = PurePosixPath(reference)
    if windows.is_absolute() or posix.is_absolute() or windows.drive or ".." in windows.parts or ".." in posix.parts:
        raise ValueError("artifact reference must be a relative path within runtime root")
    candidate = (root / Path(*posix.parts)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("artifact reference escapes runtime root") from error
    if candidate.suffix != ".json":
        raise ValueError("policy artifact references must be JSON")
    return candidate


def _load_canonical_json(path: Path) -> dict[str, Any]:
    data, _ = _load_canonical_json_with_bytes(path)
    return data


def _load_canonical_json_with_bytes(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("unable to load JSON artifact") from error
    try:
        decoded = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid JSON artifact") from error
    if not isinstance(decoded, dict):
        raise ValueError("JSON artifact root must be an object")
    canonical = _canonical_json(decoded)
    if text != canonical:
        raise ValueError("JSON artifact must use canonical JSON")
    return decoded, raw


def _validate_artifact_matches_manifest(
    artifact: Mapping[str, Any], manifest: PolicyArtifactManifest
) -> None:
    required = {
        "policy_id",
        "adapter_id",
        "adapter_version",
        "feature_schema_version",
        "action_space_version",
        "dimension",
        "alpha",
        "actions",
    }
    if set(artifact) != required:
        raise ValueError("artifact has an invalid schema")
    for name in (
        "policy_id",
        "adapter_id",
        "adapter_version",
        "feature_schema_version",
        "action_space_version",
    ):
        if artifact[name] != getattr(manifest, name):
            raise ValueError(f"artifact {name} does not match manifest")
    dimension = artifact["dimension"]
    if type(dimension) is not int or dimension <= 0:
        raise ValueError("artifact dimension must be a positive integer")
    _require_finite_nonnegative(artifact["alpha"], "artifact alpha")
    actions = artifact["actions"]
    if not isinstance(actions, dict) or not actions:
        raise ValueError("artifact actions must be a non-empty object")
    for candidate_id, parameters in actions.items():
        if not isinstance(candidate_id, str) or not candidate_id.strip() or not isinstance(parameters, dict):
            raise ValueError("artifact action is invalid")
        if set(parameters) != {"theta", "inverse_covariance"}:
            raise ValueError("artifact action parameters are invalid")
        _validate_vector(parameters["theta"], dimension, "theta")
        matrix = parameters["inverse_covariance"]
        if not isinstance(matrix, list) or len(matrix) != dimension:
            raise ValueError("artifact inverse covariance has invalid dimension")
        for row in matrix:
            _validate_vector(row, dimension, "inverse covariance")


def _validate_vector(value: Any, dimension: int, name: str) -> None:
    if not isinstance(value, list) or len(value) != dimension:
        raise ValueError(f"artifact {name} has invalid dimension")
    for number in value:
        if type(number) not in {int, float} or not math.isfinite(number):
            raise ValueError(f"artifact {name} must be finite")


def _require_finite_nonnegative(value: Any, name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


def _validate_expected_actions(actions: Mapping[str, Any], expected_action_ids: tuple[str, ...]) -> None:
    if (
        not expected_action_ids
        or len(set(expected_action_ids)) != len(expected_action_ids)
        or any(not isinstance(action_id, str) or not action_id.strip() for action_id in expected_action_ids)
    ):
        raise ValueError("expected action ids must be unique non-blank strings")
    if not set(expected_action_ids).issubset(actions):
        raise ValueError("safe candidates are missing from artifact action space")


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("JSON artifact cannot be canonicalized") from error


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON artifact has duplicate keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")
