"""Lazy adapter for administrator-provided, trusted scikit-learn artifacts.

Joblib deserialization can execute code. This loader is therefore only for a
locally configured artifact directory controlled by trusted administrators,
never for learner-provided or uploaded paths.
"""

from __future__ import annotations

import importlib
import json
import math
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from numbers import Real
from operator import index
from pathlib import Path
from typing import Any

from course_insight.modules.m4_task_orchestration.intent import (
    IntentAdapter,
    IntentPrediction,
)


_MANIFEST_SCHEMA_VERSION = 1
_MAX_MANIFEST_BYTES = 64 * 1024
_EXPECTED_LABELS = (
    "correction",
    "diagnostic",
    "out_of_scope",
    "practice",
    "qa",
    "stage_assessment",
)
_EXPECTED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "adapter_id",
        "adapter_version",
        "model_sha256",
        "labels",
        "vectorizer",
        "classifier",
        "training_provenance",
    }
)
_PROBABILITY_TOLERANCE = 1e-9


class IntentArtifactError(RuntimeError):
    """A private, path-safe failure to validate or use an intent artifact."""


@dataclass(frozen=True, slots=True)
class _IntentManifest:
    adapter_id: str
    adapter_version: str
    model_sha256: str
    labels: tuple[str, ...]
    vectorizer_type: str
    classifier_type: str


@dataclass(frozen=True, slots=True)
class SklearnIntentAdapter:
    """Immutable wrapper around a verified trusted local model pipeline."""

    _artifact: Any = field(repr=False)
    _manifest: _IntentManifest = field(repr=False)

    @property
    def adapter_id(self) -> str:
        return self._manifest.adapter_id

    @property
    def adapter_version(self) -> str:
        return self._manifest.adapter_version

    def predict(self, text: str) -> IntentPrediction:
        """Return only immutable scores and labels, never the source text."""

        if not isinstance(text, str):
            raise IntentArtifactError("model prediction input is invalid")
        try:
            raw_probabilities = self._artifact.predict_proba([text])
        except Exception:
            raise IntentArtifactError("model prediction failed") from None
        probabilities = _validated_probabilities(
            raw_probabilities,
            class_count=len(self._manifest.labels),
        )
        ranked_indices = sorted(
            range(len(probabilities)),
            key=probabilities.__getitem__,
            reverse=True,
        )
        top_index, second_index = ranked_indices[:2]
        confidence = probabilities[top_index]
        margin = confidence - probabilities[second_index]
        top_label = self._manifest.labels[top_index]
        if top_label == "out_of_scope":
            return IntentPrediction.out_of_scope(
                confidence=confidence,
                margin=margin,
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
            )
        return IntentPrediction.accepted(
            label=top_label,
            confidence=confidence,
            margin=margin,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
        )


def load_sklearn_intent_adapter(model_dir: Path) -> IntentAdapter:
    """Load a checksum-pinned trusted local artifact after strict validation."""

    root = _validated_model_root(model_dir)
    manifest_path = _contained_regular_file(root, "manifest.json")
    model_path = _contained_regular_file(root, "model.joblib")
    manifest = _parse_manifest(manifest_path)
    model_bytes = _read_model_bytes(model_path)
    if sha256(model_bytes).hexdigest() != manifest.model_sha256:
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


def _validated_model_root(model_dir: object) -> Path:
    if (
        not isinstance(model_dir, Path)
        or not model_dir.is_absolute()
        or ".." in model_dir.parts
    ):
        raise IntentArtifactError("model directory is unsafe")
    try:
        if model_dir.is_symlink():
            raise IntentArtifactError("model directory is unsafe")
        root = model_dir.resolve(strict=True)
        if not stat.S_ISDIR(root.stat().st_mode):
            raise IntentArtifactError("model directory is unavailable")
    except IntentArtifactError:
        raise
    except (OSError, RuntimeError):
        raise IntentArtifactError("model directory is unavailable") from None
    return root


def _contained_regular_file(root: Path, name: str) -> Path:
    candidate = root / name
    try:
        if candidate.is_symlink():
            raise IntentArtifactError("artifact file is unsafe")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise IntentArtifactError("artifact file is unsafe")
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise IntentArtifactError("artifact file is unavailable")
    except IntentArtifactError:
        raise
    except (OSError, RuntimeError):
        raise IntentArtifactError("artifact file is unavailable") from None
    return resolved


def _parse_manifest(path: Path) -> _IntentManifest:
    try:
        size = path.stat().st_size
        if size > _MAX_MANIFEST_BYTES:
            raise IntentArtifactError("manifest size exceeds limit")
        payload = path.read_bytes()
    except IntentArtifactError:
        raise
    except OSError:
        raise IntentArtifactError("manifest is unavailable") from None
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise IntentArtifactError("manifest size exceeds limit")
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
    if (
        isinstance(raw["schema_version"], bool)
        or raw["schema_version"] != _MANIFEST_SCHEMA_VERSION
    ):
        raise IntentArtifactError("manifest schema is unsupported")
    adapter_id = _nonblank_manifest_string(raw["adapter_id"], "adapter id")
    adapter_version = _nonblank_manifest_string(
        raw["adapter_version"],
        "adapter version",
    )
    model_sha256 = _validated_sha256(raw["model_sha256"])
    labels = raw["labels"]
    if not isinstance(labels, list) or tuple(labels) != _EXPECTED_LABELS:
        raise IntentArtifactError("manifest labels are invalid")
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
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        model_sha256=model_sha256,
        labels=tuple(labels),
        vectorizer_type=vectorizer_type,
        classifier_type=classifier_type,
    )


def _nonblank_manifest_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntentArtifactError(f"manifest {field_name} is invalid")
    return value


def _validated_sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise IntentArtifactError("manifest model sha256 is invalid")
    return value


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


def _read_model_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise IntentArtifactError("model artifact is unavailable") from None


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
    _validate_model_classes(getattr(artifact, "classes_", None), manifest.labels)
    _validate_model_classes(
        getattr(classifier, "classes_", None),
        manifest.labels,
    )
    _validate_feature_shape(vectorizer, classifier, len(manifest.labels))


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
