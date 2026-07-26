from __future__ import annotations

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pytest

from course_insight.modules.m4_task_orchestration.intent import (
    IntentAdapter,
    IntentStatus,
)
from course_insight.modules.m4_task_orchestration.sklearn_adapter import (
    IntentArtifactError,
    load_sklearn_intent_adapter,
)


EXPECTED_LABELS = (
    "correction",
    "diagnostic",
    "out_of_scope",
    "practice",
    "qa",
    "stage_assessment",
)
PRACTICE_PROBABILITIES = (0.04, 0.05, 0.03, 0.72, 0.10, 0.06)
OOS_PROBABILITIES = (0.03, 0.04, 0.80, 0.03, 0.08, 0.02)
pytestmark = pytest.mark.filterwarnings(
    "ignore:Setting the shape on a NumPy array has been deprecated:"
    "DeprecationWarning:joblib.numpy_pickle"
)


class _FixtureVectorizer:
    def __init__(self, feature_count: int = 2) -> None:
        self.feature_count = feature_count

    def get_feature_names_out(self) -> np.ndarray:
        return np.asarray(
            [f"feature-{index}" for index in range(self.feature_count)],
            dtype=object,
        )


class _FixtureClassifier:
    def __init__(
        self,
        *,
        classes: object = EXPECTED_LABELS,
        feature_count: int = 2,
    ) -> None:
        self.classes_ = np.asarray(classes, dtype=object)
        self.n_features_in_ = feature_count
        self.coef_ = np.zeros((len(EXPECTED_LABELS), feature_count))


class _FixtureArtifact:
    def __init__(
        self,
        *,
        probabilities: object = (PRACTICE_PROBABILITIES,),
        classes: object = EXPECTED_LABELS,
        vectorizer_features: int = 2,
        classifier_features: int = 2,
    ) -> None:
        self.classes_ = np.asarray(classes, dtype=object)
        self.named_steps = {
            "tfidf": _FixtureVectorizer(vectorizer_features),
            "classifier": _FixtureClassifier(
                classes=classes,
                feature_count=classifier_features,
            ),
        }
        self._probabilities = np.asarray(probabilities, dtype=float)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        del texts
        return self._probabilities


class _RaisingArtifact(_FixtureArtifact):
    def predict_proba(self, texts: list[str]) -> np.ndarray:
        raise RuntimeError(texts[0])


def _manifest(model_bytes: bytes) -> dict[str, object]:
    return {
        "schema_version": 1,
        "adapter_id": "fixture-sklearn-intent",
        "adapter_version": "1.0.0",
        "model_sha256": sha256(model_bytes).hexdigest(),
        "labels": list(EXPECTED_LABELS),
        "vectorizer": {
            "type": "_FixtureVectorizer",
            "analyzer": "char",
        },
        "classifier": {
            "type": "_FixtureClassifier",
            "class_weight": "balanced",
        },
        "training_provenance": {
            "dataset_sha256": "0" * 64,
            "seed": 17,
        },
    }


def _write_artifact(
    directory: Path,
    *,
    artifact: object | None = None,
    manifest_updates: dict[str, object] | None = None,
) -> Path:
    directory.mkdir()
    model_path = directory / "model.joblib"
    joblib.dump(_FixtureArtifact() if artifact is None else artifact, model_path)
    manifest = {
        **_manifest(model_path.read_bytes()),
        **({} if manifest_updates is None else manifest_updates),
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return directory


def _rewrite_manifest(
    directory: Path,
    **updates: object,
) -> None:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        json.dumps({**manifest, **updates}, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    return _write_artifact(tmp_path / "artifact")


def test_adapter_returns_immutable_prediction_with_top_two_scores(
    artifact_dir: Path,
) -> None:
    adapter = load_sklearn_intent_adapter(artifact_dir)

    prediction = adapter.predict("请给我一道练习题")

    assert isinstance(adapter, IntentAdapter)
    assert adapter.adapter_id == "fixture-sklearn-intent"
    assert adapter.adapter_version == "1.0.0"
    assert prediction.label == "practice"
    assert prediction.status is IntentStatus.ACCEPTED
    assert prediction.confidence == pytest.approx(0.72)
    assert prediction.margin == pytest.approx(0.62)
    assert prediction.adapter_id == "fixture-sklearn-intent"
    assert prediction.adapter_version == "1.0.0"
    with pytest.raises(AttributeError):
        prediction.label = "qa"  # type: ignore[misc]


def test_adapter_maps_top_out_of_scope_to_non_task_prediction(
    tmp_path: Path,
) -> None:
    directory = _write_artifact(
        tmp_path / "artifact",
        artifact=_FixtureArtifact(probabilities=[OOS_PROBABILITIES]),
    )
    adapter = load_sklearn_intent_adapter(directory)

    prediction = adapter.predict("帮我预订明天的机票")

    assert prediction.status is IntentStatus.OUT_OF_SCOPE
    assert prediction.label is None
    assert prediction.confidence == pytest.approx(0.80)
    assert prediction.margin == pytest.approx(0.72)


def test_adapter_rejects_model_checksum_mismatch(artifact_dir: Path) -> None:
    (artifact_dir / "model.joblib").write_bytes(b"tampered")

    with pytest.raises(IntentArtifactError, match="checksum"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_deserializes_the_same_bytes_that_passed_checksum(
    artifact_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = artifact_dir / "model.joblib"
    replacement_path = tmp_path / "replacement.joblib"
    joblib.dump(
        _FixtureArtifact(probabilities=[OOS_PROBABILITIES]),
        replacement_path,
    )
    replacement_bytes = replacement_path.read_bytes()
    original_load = joblib.load

    def swap_path_then_load(file_object: object, *args: Any, **kwargs: Any) -> Any:
        model_path.write_bytes(replacement_bytes)
        return original_load(file_object, *args, **kwargs)

    monkeypatch.setattr(joblib, "load", swap_path_then_load)

    prediction = load_sklearn_intent_adapter(artifact_dir).predict("练习")

    assert prediction.status is IntentStatus.ACCEPTED
    assert prediction.label == "practice"


def test_adapter_rejects_hardlinked_model_artifact(
    artifact_dir: Path,
    tmp_path: Path,
) -> None:
    model_path = artifact_dir / "model.joblib"
    outside_model = tmp_path / "outside-model.joblib"
    outside_model.write_bytes(model_path.read_bytes())
    model_path.unlink()
    os.link(outside_model, model_path)

    with pytest.raises(IntentArtifactError, match="unsafe"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_path_replacement_between_validation_and_open(
    artifact_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_dir = _write_artifact(
        tmp_path / "replacement",
        artifact=_FixtureArtifact(probabilities=[OOS_PROBABILITIES]),
    )
    manifest_path = artifact_dir / "manifest.json"
    model_path = artifact_dir / "model.joblib"
    original_open = Path.open
    swapped = False

    def swap_pair_then_open(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal swapped
        if path == manifest_path and not swapped:
            swapped = True
            os.replace(manifest_path, artifact_dir / "original-manifest.json")
            os.replace(model_path, artifact_dir / "original-model.joblib")
            os.replace(replacement_dir / "manifest.json", manifest_path)
            os.replace(replacement_dir / "model.joblib", model_path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_pair_then_open)

    with pytest.raises(IntentArtifactError, match="changed"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_model_changed_while_being_read(
    artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = artifact_dir / "model.joblib"
    original_open = Path.open

    class MutatingReader:
        def __init__(self, path: Path) -> None:
            self._handle = original_open(path, "r+b")

        def __enter__(self) -> MutatingReader:
            return self

        def __exit__(self, *args: object) -> None:
            self._handle.close()

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self, size: int = -1) -> bytes:
            payload = self._handle.read(size)
            os.ftruncate(self._handle.fileno(), len(payload) + 1)
            return payload

    def mutating_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == model_path:
            return MutatingReader(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mutating_open)

    with pytest.raises(IntentArtifactError, match="changed"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_oversized_model_before_reading(
    artifact_dir: Path,
) -> None:
    with (artifact_dir / "model.joblib").open("r+b") as model_file:
        model_file.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(IntentArtifactError, match="model size"):
        load_sklearn_intent_adapter(artifact_dir)


@pytest.mark.parametrize("missing_name", ["manifest.json", "model.joblib"])
def test_adapter_rejects_missing_artifact_files(
    artifact_dir: Path,
    missing_name: str,
) -> None:
    (artifact_dir / missing_name).unlink()

    with pytest.raises(IntentArtifactError, match="unavailable"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_missing_or_non_directory_root(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    regular_file = tmp_path / "regular-file"
    regular_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(IntentArtifactError, match="directory"):
        load_sklearn_intent_adapter(missing)
    with pytest.raises(IntentArtifactError, match="directory"):
        load_sklearn_intent_adapter(regular_file)


def test_adapter_rejects_model_directory_with_parent_traversal(
    artifact_dir: Path,
) -> None:
    traversing_path = artifact_dir / ".." / artifact_dir.name

    with pytest.raises(IntentArtifactError, match="directory"):
        load_sklearn_intent_adapter(traversing_path)


@pytest.mark.parametrize("artifact_name", ["manifest.json", "model.joblib"])
def test_adapter_rejects_symlink_escape_before_deserialization(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    directory = _write_artifact(tmp_path / "artifact")
    outside = tmp_path / f"outside-{artifact_name}"
    outside.mkdir()
    (directory / artifact_name).unlink()
    try:
        (directory / artifact_name).symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"symlink creation unavailable: {error}")
        subprocess.run(
            [
                "cmd",
                "/c",
                "mklink",
                "/J",
                os.fspath(directory / artifact_name),
                os.fspath(outside),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    with pytest.raises(IntentArtifactError, match="unsafe"):
        load_sklearn_intent_adapter(directory)


@pytest.mark.parametrize("artifact_name", ["manifest.json", "model.joblib"])
def test_adapter_rejects_non_regular_artifact_files(
    artifact_dir: Path,
    artifact_name: str,
) -> None:
    (artifact_dir / artifact_name).unlink()
    (artifact_dir / artifact_name).mkdir()

    with pytest.raises(IntentArtifactError, match="unavailable"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_oversized_manifest_before_deserialization(
    artifact_dir: Path,
) -> None:
    (artifact_dir / "manifest.json").write_bytes(b" " * (64 * 1024 + 1))

    with pytest.raises(IntentArtifactError, match="size"):
        load_sklearn_intent_adapter(artifact_dir)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xff", "UTF-8"),
        (b"{", "JSON"),
        (b'{"schema_version": NaN}', "JSON"),
        (
            b'{"schema_version":1,"schema_version":1}',
            "duplicate",
        ),
    ],
)
def test_adapter_rejects_noncanonical_manifest_encodings(
    artifact_dir: Path,
    payload: bytes,
    message: str,
) -> None:
    (artifact_dir / "manifest.json").write_bytes(payload)

    with pytest.raises(IntentArtifactError, match=message):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_unsupported_manifest_schema(
    artifact_dir: Path,
) -> None:
    _rewrite_manifest(artifact_dir, schema_version=2)

    with pytest.raises(IntentArtifactError, match="schema"):
        load_sklearn_intent_adapter(artifact_dir)


def test_adapter_rejects_unknown_manifest_fields(artifact_dir: Path) -> None:
    _rewrite_manifest(artifact_dir, unexpected="value")

    with pytest.raises(IntentArtifactError, match="fields"):
        load_sklearn_intent_adapter(artifact_dir)


@pytest.mark.parametrize(
    "labels",
    [
        [*EXPECTED_LABELS[:-1], "unknown"],
        [*EXPECTED_LABELS[:-1], EXPECTED_LABELS[-2]],
        list(EXPECTED_LABELS[:-1]),
        list(reversed(EXPECTED_LABELS)),
    ],
)
def test_adapter_requires_exact_ordered_six_label_manifest(
    artifact_dir: Path,
    labels: list[str],
) -> None:
    _rewrite_manifest(artifact_dir, labels=labels)

    with pytest.raises(IntentArtifactError, match="labels"):
        load_sklearn_intent_adapter(artifact_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adapter_id", ""),
        ("adapter_version", " "),
        ("model_sha256", "not-a-checksum"),
        ("vectorizer", {}),
        ("classifier", {"type": ""}),
        ("training_provenance", {}),
    ],
)
def test_adapter_rejects_invalid_required_manifest_metadata(
    artifact_dir: Path,
    field: str,
    value: object,
) -> None:
    _rewrite_manifest(artifact_dir, **{field: value})

    with pytest.raises(IntentArtifactError, match=field.replace("_", " ")):
        load_sklearn_intent_adapter(artifact_dir)


@pytest.mark.parametrize(
    "classes",
    [
        list(reversed(EXPECTED_LABELS)),
        [*EXPECTED_LABELS[:-1], EXPECTED_LABELS[-2]],
        np.asarray([EXPECTED_LABELS], dtype=object),
    ],
)
def test_adapter_rejects_malformed_or_reordered_model_classes(
    tmp_path: Path,
    classes: object,
) -> None:
    directory = _write_artifact(
        tmp_path / "artifact",
        artifact=_FixtureArtifact(classes=classes),
    )

    with pytest.raises(IntentArtifactError, match="classes"):
        load_sklearn_intent_adapter(directory)


def test_adapter_rejects_vectorizer_classifier_shape_mismatch(
    tmp_path: Path,
) -> None:
    directory = _write_artifact(
        tmp_path / "artifact",
        artifact=_FixtureArtifact(
            vectorizer_features=3,
            classifier_features=2,
        ),
    )

    with pytest.raises(IntentArtifactError, match="feature shape"):
        load_sklearn_intent_adapter(directory)


@pytest.mark.parametrize(
    "probabilities",
    [
        PRACTICE_PROBABILITIES,
        [PRACTICE_PROBABILITIES, PRACTICE_PROBABILITIES],
        [[0.04, 0.05, 0.03, 0.72, 0.16]],
        [[0.04, 0.05, np.nan, 0.72, 0.10, 0.09]],
        [[0.04, 0.05, np.inf, 0.72, 0.10, 0.09]],
        [[-0.01, 0.05, 0.04, 0.72, 0.10, 0.10]],
        [[0.04, 0.05, 0.03, 1.02, 0.10, 0.06]],
        [[0.04, 0.05, 0.03, 0.62, 0.10, 0.06]],
    ],
)
def test_adapter_rejects_invalid_probability_arrays(
    tmp_path: Path,
    probabilities: object,
) -> None:
    directory = _write_artifact(
        tmp_path / "artifact",
        artifact=_FixtureArtifact(probabilities=probabilities),
    )
    adapter = load_sklearn_intent_adapter(directory)

    with pytest.raises(IntentArtifactError, match="probabilities"):
        adapter.predict("不应出现在错误信息中的原始文本")


def test_artifact_errors_do_not_expose_configured_paths(
    artifact_dir: Path,
) -> None:
    sensitive_name = artifact_dir.name
    (artifact_dir / "model.joblib").write_bytes(b"tampered")

    with pytest.raises(IntentArtifactError) as captured:
        load_sklearn_intent_adapter(artifact_dir)

    assert sensitive_name not in str(captured.value)
    assert str(artifact_dir) not in str(captured.value)


def test_prediction_errors_do_not_expose_raw_text(tmp_path: Path) -> None:
    raw_text = "PRIVATE-LEARNER-TEXT-DO-NOT-LOG"
    directory = _write_artifact(
        tmp_path / "artifact",
        artifact=_RaisingArtifact(),
    )
    adapter = load_sklearn_intent_adapter(directory)

    with pytest.raises(IntentArtifactError) as captured:
        adapter.predict(raw_text)

    assert raw_text not in str(captured.value)
    assert captured.value.__cause__ is None


def test_adapter_rejects_path_values_that_are_not_path_objects() -> None:
    with pytest.raises(IntentArtifactError, match="directory"):
        load_sklearn_intent_adapter("artifact")  # type: ignore[arg-type]


def test_adapter_rejects_inaccessible_artifact_without_path_details(
    artifact_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = artifact_dir / "model.joblib"
    original_open = Path.open

    def denied_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == model_path:
            raise PermissionError("private filesystem detail")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)

    with pytest.raises(IntentArtifactError, match="unavailable") as captured:
        load_sklearn_intent_adapter(artifact_dir)

    assert "private filesystem detail" not in str(captured.value)
    assert os.fspath(model_path) not in str(captured.value)
    assert captured.value.__cause__ is None
