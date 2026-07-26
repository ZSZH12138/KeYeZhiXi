"""Lazy adapter for administrator-provided, trusted scikit-learn artifacts.

Joblib deserialization can execute code. This loader is therefore only for a
locally configured artifact directory controlled by trusted administrators,
never for learner-provided or uploaded paths.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from numbers import Real
from operator import index
from pathlib import Path
from typing import Any

from course_insight.modules.m4_task_orchestration.intent import (
    SUPPORTED_INTENT_LABELS,
    IntentAdapter,
    IntentPrediction,
)
from course_insight.modules.m4_task_orchestration.normalization import (
    NORMALIZATION_VERSION,
    normalize_intent_text,
)


_MANIFEST_SCHEMA_VERSION = "1"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_MODEL_BYTES = 64 * 1024 * 1024
_EXPECTED_LABELS = (
    "correction",
    "diagnostic",
    "out_of_scope",
    "practice",
    "qa",
    "stage_assessment",
)
_EXPECTED_PUBLIC_LABELS = SUPPORTED_INTENT_LABELS
_EXPECTED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "model_id",
        "model_version",
        "adapter_type",
        "supported_task_types",
        "normalization_version",
        "training_dataset_checksum",
        "model_artifact_checksum",
        "library_versions",
        "created_at",
        "vectorizer",
        "classifier",
        "training_provenance",
    }
)
_PROBABILITY_TOLERANCE = 1e-9
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_EXPECTED_LIBRARY_FIELDS = frozenset(
    {"python", "scikit_learn", "joblib"}
)


class IntentArtifactError(RuntimeError):
    """A private, path-safe failure to validate or use an intent artifact."""


@dataclass(frozen=True, slots=True)
class _IntentManifest:
    model_id: str
    model_version: str
    model_artifact_checksum: str
    training_dataset_checksum: str
    vectorizer_type: str
    classifier_type: str


@dataclass(frozen=True, slots=True)
class SklearnIntentAdapter:
    """Immutable wrapper around a verified trusted local model pipeline."""

    _artifact: Any = field(repr=False)
    _manifest: _IntentManifest = field(repr=False)

    @property
    def adapter_id(self) -> str:
        return self._manifest.model_id

    @property
    def adapter_version(self) -> str:
        return (
            f"{self._manifest.model_version}+sha256."
            f"{self._manifest.model_artifact_checksum}"
        )

    def predict(self, text: str) -> IntentPrediction:
        """Return only immutable scores and labels, never the source text."""

        if not isinstance(text, str):
            raise IntentArtifactError("model prediction input is invalid")
        try:
            normalized_text = normalize_intent_text(text)
            if not normalized_text:
                raise IntentArtifactError("model prediction input is invalid")
            raw_probabilities = self._artifact.predict_proba([normalized_text])
        except Exception:
            raise IntentArtifactError("model prediction failed") from None
        probabilities = _validated_probabilities(
            raw_probabilities,
            class_count=len(_EXPECTED_LABELS),
        )
        ranked_indices = sorted(
            range(len(probabilities)),
            key=probabilities.__getitem__,
            reverse=True,
        )
        top_index, second_index = ranked_indices[:2]
        confidence = probabilities[top_index]
        margin = confidence - probabilities[second_index]
        top_label = _EXPECTED_LABELS[top_index]
        second_label = _EXPECTED_LABELS[second_index]
        public_scores = {
            label: probabilities[_EXPECTED_LABELS.index(label)]
            for label in _EXPECTED_PUBLIC_LABELS
        }
        if "out_of_scope" in {top_label, second_label}:
            return IntentPrediction.out_of_scope(
                scores=public_scores,
                confidence=confidence,
                margin=margin,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                reason_codes=(
                    ("outside_supported_scope",)
                    if top_label == "out_of_scope"
                    else ("out_of_scope_competitor",)
                ),
            )
        return IntentPrediction.accepted(
            label=top_label,
            scores=public_scores,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )


def load_sklearn_intent_adapter(
    model_dir: Path,
    *,
    runtime_dir: Path,
    expected_model_id: str,
    expected_model_version: str,
    expected_model_sha256: str,
) -> IntentAdapter:
    """Load a checksum-pinned trusted local artifact after strict validation."""

    _validated_external_identity(
        expected_model_id,
        expected_model_version,
        expected_model_sha256,
    )
    root = _validated_model_root(model_dir, runtime_dir=runtime_dir)
    manifest_bytes = _read_stable_artifact_file(
        root,
        "manifest.json",
        max_bytes=_MAX_MANIFEST_BYTES,
        file_kind="manifest",
    )
    model_bytes = _read_stable_artifact_file(
        root,
        "model.joblib",
        max_bytes=_MAX_MODEL_BYTES,
        file_kind="model",
    )
    manifest = _parse_manifest(manifest_bytes)
    if (
        manifest.model_id != expected_model_id
        or manifest.model_version != expected_model_version
    ):
        raise IntentArtifactError("model identity mismatch")
    model_checksum = sha256(model_bytes).hexdigest()
    if (
        model_checksum != manifest.model_artifact_checksum
        or model_checksum != expected_model_sha256
    ):
        raise IntentArtifactError("model checksum mismatch")

    try:
        joblib = importlib.import_module("joblib")
    except (ImportError, ModuleNotFoundError):
        raise IntentArtifactError(
            "optional intent dependencies are unavailable"
        ) from None
    try:
        artifact = joblib.load(BytesIO(model_bytes))
    except ModuleNotFoundError as error:
        if _is_optional_dependency(error.name):
            raise IntentArtifactError(
                "optional intent dependencies are unavailable"
            ) from None
        raise IntentArtifactError(
            "trusted model artifact could not be deserialized"
        ) from None
    except Exception:
        raise IntentArtifactError(
            "trusted model artifact could not be deserialized"
        ) from None

    _validate_artifact(artifact, manifest)
    return SklearnIntentAdapter(_artifact=artifact, _manifest=manifest)


def _validated_external_identity(
    model_id: object,
    model_version: object,
    model_sha256: object,
) -> None:
    if (
        not isinstance(model_id, str)
        or _SAFE_IDENTIFIER.fullmatch(model_id) is None
        or not isinstance(model_version, str)
        or len(model_version) > 56
        or _SAFE_IDENTIFIER.fullmatch(model_version) is None
    ):
        raise IntentArtifactError("model identity is invalid")
    _validated_sha256(model_sha256, field_name="configured model sha256")


def _validated_model_root(model_dir: object, *, runtime_dir: object) -> Path:
    if (
        not isinstance(model_dir, Path)
        or not model_dir.is_absolute()
        or ".." in model_dir.parts
        or not isinstance(runtime_dir, Path)
        or not runtime_dir.is_absolute()
        or ".." in runtime_dir.parts
    ):
        raise IntentArtifactError("model directory is unsafe")
    try:
        runtime_root = runtime_dir.resolve(strict=True)
        if not stat.S_ISDIR(runtime_root.stat().st_mode):
            raise IntentArtifactError("model directory is unavailable")
        if model_dir.is_symlink():
            raise IntentArtifactError("model directory is unsafe")
        root = model_dir.resolve(strict=True)
        if root == runtime_root or not root.is_relative_to(runtime_root):
            raise IntentArtifactError("model directory is unsafe")
        if not stat.S_ISDIR(root.stat().st_mode):
            raise IntentArtifactError("model directory is unavailable")
    except IntentArtifactError:
        raise
    except (OSError, RuntimeError):
        raise IntentArtifactError("model directory is unavailable") from None
    return root


def _read_stable_artifact_file(
    root: Path,
    name: str,
    *,
    max_bytes: int,
    file_kind: str,
) -> bytes:
    candidate = root / name
    try:
        validated = candidate.lstat()
        if stat.S_ISLNK(validated.st_mode) or candidate.is_symlink():
            raise IntentArtifactError("artifact file is unsafe")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise IntentArtifactError("artifact file is unsafe")
        if not stat.S_ISREG(validated.st_mode):
            raise IntentArtifactError("artifact file is unavailable")
        _require_single_link(validated)
        _require_size_within_limit(validated.st_size, max_bytes, file_kind)

        with candidate.open("rb") as artifact_file:
            opened = os.fstat(artifact_file.fileno())
            _require_stable_snapshot(validated, opened, file_kind)
            _require_single_link(opened)
            _require_size_within_limit(opened.st_size, max_bytes, file_kind)
            payload = artifact_file.read(max_bytes + 1)
            after_read = os.fstat(artifact_file.fileno())

        if len(payload) > max_bytes:
            _raise_size_error(file_kind)
        _require_stable_snapshot(opened, after_read, file_kind)
        _require_single_link(after_read)
        if len(payload) != opened.st_size:
            _raise_changed_error(file_kind)
        return payload
    except IntentArtifactError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise IntentArtifactError("artifact file is unavailable") from None


def _require_single_link(file_stat: os.stat_result) -> None:
    if file_stat.st_nlink != 1:
        raise IntentArtifactError("artifact file is unsafe")


def _require_size_within_limit(
    size: int,
    max_bytes: int,
    file_kind: str,
) -> None:
    if size < 0 or size > max_bytes:
        _raise_size_error(file_kind)


def _require_stable_snapshot(
    expected: os.stat_result,
    actual: os.stat_result,
    file_kind: str,
) -> None:
    if (
        not stat.S_ISREG(actual.st_mode)
        or _snapshot_identity(expected) != _snapshot_identity(actual)
    ):
        _raise_changed_error(file_kind)


def _snapshot_identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _raise_size_error(file_kind: str) -> None:
    raise IntentArtifactError(f"{file_kind} size exceeds limit")


def _raise_changed_error(file_kind: str) -> None:
    raise IntentArtifactError(f"{file_kind} artifact changed during validation")


def _parse_manifest(payload: bytes) -> _IntentManifest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise IntentArtifactError("manifest must be UTF-8") from None
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateManifestKey:
        raise IntentArtifactError("manifest contains duplicate fields") from None
    except (json.JSONDecodeError, ValueError):
        raise IntentArtifactError("manifest is not valid JSON") from None
    return _validated_manifest(raw)


def _validated_manifest(raw: object) -> _IntentManifest:
    if not isinstance(raw, dict) or set(raw) != _EXPECTED_MANIFEST_FIELDS:
        raise IntentArtifactError("manifest fields are invalid")
    if raw["schema_version"] != _MANIFEST_SCHEMA_VERSION:
        raise IntentArtifactError("manifest schema is unsupported")
    model_id = _safe_manifest_identifier(raw["model_id"], "model id")
    model_version = _safe_manifest_identifier(
        raw["model_version"],
        "model version",
    )
    if raw["adapter_type"] != "sklearn":
        raise IntentArtifactError("manifest adapter type is invalid")
    labels = raw["supported_task_types"]
    if (
        not isinstance(labels, list)
        or tuple(labels) != _EXPECTED_PUBLIC_LABELS
    ):
        raise IntentArtifactError("manifest labels are invalid")
    if raw["normalization_version"] != NORMALIZATION_VERSION:
        raise IntentArtifactError(
            "manifest normalization version is invalid"
        )
    training_dataset_checksum = _validated_sha256(
        raw["training_dataset_checksum"],
        field_name="training dataset checksum",
    )
    model_artifact_checksum = _validated_sha256(
        raw["model_artifact_checksum"],
        field_name="model artifact checksum",
    )
    _validated_library_versions(raw["library_versions"])
    _validated_created_at(raw["created_at"])
    vectorizer_type = _metadata_type(raw["vectorizer"], "vectorizer")
    classifier_type = _metadata_type(raw["classifier"], "classifier")
    provenance = raw["training_provenance"]
    if not isinstance(provenance, dict) or not provenance:
        raise IntentArtifactError(
            "manifest training provenance is invalid"
        )
    if any(not isinstance(key, str) or not key.strip() for key in provenance):
        raise IntentArtifactError(
            "manifest training provenance is invalid"
        )
    return _IntentManifest(
        model_id=model_id,
        model_version=model_version,
        model_artifact_checksum=model_artifact_checksum,
        training_dataset_checksum=training_dataset_checksum,
        vectorizer_type=vectorizer_type,
        classifier_type=classifier_type,
    )


def _safe_manifest_identifier(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_IDENTIFIER.fullmatch(value) is None
        or (field_name == "model version" and len(value) > 56)
    ):
        raise IntentArtifactError(f"manifest {field_name} is invalid")
    return value


def _validated_sha256(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IntentArtifactError(f"{field_name} is invalid")
    return value


def _validated_library_versions(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != _EXPECTED_LIBRARY_FIELDS
        or any(
            not isinstance(version, str)
            or not version
            or len(version) > 128
            or any(character.isspace() for character in version)
            for version in value.values()
        )
    ):
        raise IntentArtifactError("manifest library versions are invalid")


def _validated_created_at(value: object) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise IntentArtifactError("manifest created at is invalid")
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        raise IntentArtifactError("manifest created at is invalid") from None
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise IntentArtifactError("manifest created at is invalid")


def _metadata_type(value: object, field_name: str) -> str:
    if not isinstance(value, dict) or not value:
        raise IntentArtifactError(
            f"manifest {field_name} metadata is invalid"
        )
    metadata_type = value.get("type")
    if not isinstance(metadata_type, str) or not metadata_type.strip():
        raise IntentArtifactError(
            f"manifest {field_name} metadata is invalid"
        )
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise IntentArtifactError(
            f"manifest {field_name} metadata is invalid"
        )
    return metadata_type


def _validate_artifact(artifact: object, manifest: _IntentManifest) -> None:
    predictor = getattr(artifact, "predict_proba", None)
    named_steps = getattr(artifact, "named_steps", None)
    if not callable(predictor) or not isinstance(named_steps, Mapping):
        raise IntentArtifactError("model artifact shape is invalid")
    if "tfidf" not in named_steps or "classifier" not in named_steps:
        raise IntentArtifactError("model artifact shape is invalid")

    vectorizer = named_steps["tfidf"]
    classifier = named_steps["classifier"]
    if (
        type(vectorizer).__name__ != manifest.vectorizer_type
        or type(classifier).__name__ != manifest.classifier_type
    ):
        raise IntentArtifactError("model component types are invalid")
    _validate_model_classes(getattr(artifact, "classes_", None), _EXPECTED_LABELS)
    _validate_model_classes(
        getattr(classifier, "classes_", None),
        _EXPECTED_LABELS,
    )
    _validate_feature_shape(vectorizer, classifier, len(_EXPECTED_LABELS))


def _validate_model_classes(
    raw_classes: object,
    expected: tuple[str, ...],
) -> None:
    classes = _plain_sequence(raw_classes)
    if (
        classes is None
        or any(not isinstance(label, str) for label in classes)
        or tuple(classes) != expected
    ):
        raise IntentArtifactError("model classes are invalid")


def _validate_feature_shape(
    vectorizer: object,
    classifier: object,
    class_count: int,
) -> None:
    get_features = getattr(vectorizer, "get_feature_names_out", None)
    if not callable(get_features):
        raise IntentArtifactError("model feature shape is invalid")
    try:
        vectorizer_features = len(get_features())
        classifier_features = index(getattr(classifier, "n_features_in_"))
        coefficient_shape = tuple(getattr(classifier, "coef_").shape)
    except (AttributeError, TypeError, ValueError):
        raise IntentArtifactError("model feature shape is invalid") from None
    if (
        vectorizer_features <= 0
        or classifier_features != vectorizer_features
        or len(coefficient_shape) != 2
        or coefficient_shape != (class_count, vectorizer_features)
    ):
        raise IntentArtifactError("model feature shape is invalid")


def _validated_probabilities(
    raw_probabilities: object,
    *,
    class_count: int,
) -> tuple[float, ...]:
    matrix = _plain_sequence(raw_probabilities)
    if matrix is None or len(matrix) != 1:
        raise IntentArtifactError("model probabilities are invalid")
    row = _plain_sequence(matrix[0])
    if row is None or len(row) != class_count:
        raise IntentArtifactError("model probabilities are invalid")
    probabilities: list[float] = []
    for value in row:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise IntentArtifactError("model probabilities are invalid")
        probability = float(value)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise IntentArtifactError("model probabilities are invalid")
        probabilities.append(probability)
    if not math.isclose(
        math.fsum(probabilities),
        1.0,
        rel_tol=_PROBABILITY_TOLERANCE,
        abs_tol=_PROBABILITY_TOLERANCE,
    ):
        raise IntentArtifactError("model probabilities are invalid")
    return tuple(probabilities)


def _plain_sequence(value: object) -> Sequence[object] | None:
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        value = to_list()
    if isinstance(value, (list, tuple)):
        return value
    return None


def _is_optional_dependency(module_name: str | None) -> bool:
    if not isinstance(module_name, str):
        return False
    return module_name.partition(".")[0] in {"joblib", "sklearn"}


class _DuplicateManifestKey(ValueError):
    pass


def _unique_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateManifestKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
