"""Train, evaluate, and atomically publish an offline M4 intent artifact."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import joblib
import sklearn
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
from sklearn.pipeline import Pipeline

from course_insight.modules.m4_task_orchestration.intent import (
    SUPPORTED_INTENT_LABELS,
)
from course_insight.modules.m4_task_orchestration.intent_dataset import (
    EXPECTED_LABELS,
    DatasetPartitions,
    IntentDatasetError,
    IntentExample,
    load_dataset,
    split_by_group,
)
from course_insight.modules.m4_task_orchestration.normalization import (
    NORMALIZATION_VERSION,
    normalize_intent_text,
)


_SCHEMA_VERSION = "1"
_MAX_MODEL_BYTES = 64 * 1024 * 1024
_PUBLIC_TASK_LABELS = SUPPORTED_INTENT_LABELS
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRODUCTION_THRESHOLDS = {
    "task_macro_f1": 0.90,
    "minimum_task_recall": 0.85,
    "stage_assessment_precision": 0.95,
    "correction_precision": 0.95,
    "out_of_scope_recall": 0.90,
    "selective_accuracy": 0.97,
}
_SAMPLE_DATASET_SHA256S = frozenset(
    {
        (
            "10100b47370610a9b6e7b59123369d37"
            "864359ef3155ad4b940faf2a4d9400e0"
        ),
        (
            "ac3258fbd08243d1770907bf747c18ad"
            "4ea749d4a494c633c3655fe3311528f0"
        ),
    }
)


class TrainingError(RuntimeError):
    """A sanitized offline-training or artifact-publication failure."""


def train_and_publish(
    *,
    input_path: Path,
    output_dir: Path,
    runtime_dir: Path,
    model_id: str,
    model_version: str,
    seed: int,
    min_confidence: float,
    min_margin: float,
) -> dict[str, Any]:
    """Train once and atomically publish a Task 5-compatible artifact."""

    _validate_options(
        input_path=input_path,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        model_id=model_id,
        model_version=model_version,
        seed=seed,
        min_confidence=min_confidence,
        min_margin=min_margin,
    )
    runtime_root = runtime_dir.resolve(strict=True)
    output = output_dir.resolve(strict=False)
    if output == runtime_root or not output.is_relative_to(runtime_root):
        raise TrainingError("output directory must remain within runtime")
    if os.path.lexists(output):
        raise TrainingError("output directory already exists")
    parent = output.parent
    if not parent.is_dir():
        raise TrainingError("output parent directory is unavailable")

    loaded = load_dataset(input_path)
    partitions = split_by_group(loaded.examples, seed=seed)
    pipeline = _fit_pipeline(partitions.train, seed=seed)
    thresholds = {
        "min_confidence": float(min_confidence),
        "min_margin": float(min_margin),
    }
    sample_only = _is_sample_dataset(
        input_path,
        dataset_sha256=loaded.sha256,
    )
    metrics = _evaluate_all(
        pipeline,
        partitions,
        seed=seed,
        thresholds=thresholds,
        sample_only=sample_only,
    )
    manifest_base = _manifest_without_model_checksum(
        dataset_sha256=loaded.sha256,
        seed=seed,
        thresholds=thresholds,
        partitions=partitions,
        sample_only=sample_only,
        model_id=model_id,
        model_version=model_version,
    )
    _publish(
        output=output,
        pipeline=pipeline,
        manifest_base=manifest_base,
        metrics=metrics,
    )
    return metrics


def _fit_pipeline(
    examples: Sequence[IntentExample],
    *,
    seed: int,
) -> Pipeline:
    pipeline = Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    analyzer="char",
                    ngram_range=(2, 5),
                    lowercase=False,
                    sublinear_tf=True,
                ),
            ),
            (
                "classifier",
                LogisticRegression(
                    max_iter=2000,
                    class_weight="balanced",
                    random_state=seed,
                ),
            ),
        ]
    )
    try:
        pipeline.fit(
            [normalize_intent_text(example.text) for example in examples],
            [example.label for example in examples],
        )
    except Exception:
        raise TrainingError("intent model training failed") from None
    if tuple(pipeline.classes_) != EXPECTED_LABELS:
        raise TrainingError("trained model labels are invalid")
    return pipeline


def _evaluate_all(
    pipeline: Pipeline,
    partitions: DatasetPartitions,
    *,
    seed: int,
    thresholds: dict[str, float],
    sample_only: bool,
) -> dict[str, Any]:
    reports = {
        "train": _evaluate_partition(
            pipeline,
            partitions.train,
            group_count=len(partitions.train_groups),
            thresholds=thresholds,
        ),
        "validation": _evaluate_partition(
            pipeline,
            partitions.validation,
            group_count=len(partitions.validation_groups),
            thresholds=thresholds,
        ),
        "test": _evaluate_partition(
            pipeline,
            partitions.test,
            group_count=len(partitions.test_groups),
            thresholds=thresholds,
        ),
    }
    train_groups = set(partitions.train_groups)
    validation_groups = set(partitions.validation_groups)
    test_groups = set(partitions.test_groups)
    production_gate = _production_gate(
        reports["test"],
        sample_only=sample_only,
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "evidence_scope": (
            "sample-only" if sample_only else "offline-dataset"
        ),
        "seed": seed,
        "thresholds": thresholds,
        "threshold_selection": {
            "source": "cli",
            "reference_partition": "validation",
            "tuned": False,
        },
        "counts": {
            "examples": sum(
                len(partition)
                for partition in (
                    partitions.train,
                    partitions.validation,
                    partitions.test,
                )
            ),
            "groups": len(
                train_groups | validation_groups | test_groups
            ),
        },
        "group_overlap": {
            "train_validation": sorted(train_groups & validation_groups),
            "train_test": sorted(train_groups & test_groups),
            "validation_test": sorted(validation_groups & test_groups),
            "has_overlap": bool(
                train_groups & validation_groups
                or train_groups & test_groups
                or validation_groups & test_groups
            ),
        },
        "partitions": reports,
        "production_gate": production_gate,
    }


def _evaluate_partition(
    pipeline: Pipeline,
    examples: Sequence[IntentExample],
    *,
    group_count: int,
    thresholds: dict[str, float],
) -> dict[str, Any]:
    truth = [example.label for example in examples]
    try:
        probability_rows = pipeline.predict_proba(
            [normalize_intent_text(example.text) for example in examples]
        )
    except Exception:
        raise TrainingError("intent model evaluation failed") from None

    predicted: list[str] = []
    accepted_predictions: list[bool] = []
    for row in probability_rows:
        ranked = sorted(
            range(len(EXPECTED_LABELS)),
            key=row.__getitem__,
            reverse=True,
        )
        top_index, second_index = ranked[:2]
        confidence = float(row[top_index])
        margin = confidence - float(row[second_index])
        predicted_label = EXPECTED_LABELS[top_index]
        predicted.append(predicted_label)
        accepted_predictions.append(
            predicted_label != "out_of_scope"
            and confidence >= thresholds["min_confidence"]
            and margin >= thresholds["min_margin"]
        )
    precision, recall, f1, support = precision_recall_fscore_support(
        truth,
        predicted,
        labels=list(EXPECTED_LABELS),
        zero_division=0.0,
    )
    per_class = {
        label: {
            "precision": float(precision[index]),
            "recall": float(recall[index]),
            "f1": float(f1[index]),
            "support": int(support[index]),
        }
        for index, label in enumerate(EXPECTED_LABELS)
    }
    correct = [
        predicted_label == truth_label
        for predicted_label, truth_label in zip(
            predicted,
            truth,
            strict=True,
        )
    ]
    task_truth = [
        truth_label != "out_of_scope"
        for truth_label in truth
    ]
    covered_task = [
        is_task and is_covered
        for is_task, is_covered in zip(
            task_truth,
            accepted_predictions,
            strict=True,
        )
    ]
    covered_count = sum(covered_task)
    accepted_count = sum(accepted_predictions)
    selective_correct = sum(
        is_accepted and is_correct
        for is_accepted, is_correct in zip(
            accepted_predictions,
            correct,
            strict=True,
        )
    )
    label_counts = {
        label: truth.count(label)
        for label in EXPECTED_LABELS
    }
    task_f1 = [
        per_class[label]["f1"]
        for label in _PUBLIC_TASK_LABELS
    ]
    matrix = confusion_matrix(
        truth,
        predicted,
        labels=list(EXPECTED_LABELS),
    )
    task_example_count = sum(task_truth)
    return {
        "task_macro_f1": float(sum(task_f1) / len(_PUBLIC_TASK_LABELS)),
        "per_class": per_class,
        "out_of_scope_recall": per_class["out_of_scope"]["recall"],
        "task_coverage": (
            covered_count / task_example_count
            if task_example_count
            else None
        ),
        "selective_accuracy": (
            selective_correct / accepted_count
            if accepted_count
            else None
        ),
        "confusion_matrix": {
            "labels": list(EXPECTED_LABELS),
            "matrix": [
                [int(value) for value in row]
                for row in matrix.tolist()
            ],
        },
        "counts": {
            "examples": len(examples),
            "in_scope_examples": task_example_count,
            "groups": group_count,
            "covered_in_scope": covered_count,
            "accepted_predictions": accepted_count,
            "selective_correct": selective_correct,
            "correct": sum(correct),
            "labels": label_counts,
        },
    }


def _production_gate(
    test_report: dict[str, Any],
    *,
    sample_only: bool,
) -> dict[str, Any]:
    per_class = test_report["per_class"]
    selective_accuracy = test_report["selective_accuracy"]
    checks = {
        "task_macro_f1": (
            test_report["task_macro_f1"]
            >= _PRODUCTION_THRESHOLDS["task_macro_f1"]
        ),
        "minimum_task_recall": all(
            per_class[label]["recall"]
            >= _PRODUCTION_THRESHOLDS["minimum_task_recall"]
            for label in _PUBLIC_TASK_LABELS
        ),
        "stage_assessment_precision": (
            per_class["stage_assessment"]["precision"]
            >= _PRODUCTION_THRESHOLDS["stage_assessment_precision"]
        ),
        "correction_precision": (
            per_class["correction"]["precision"]
            >= _PRODUCTION_THRESHOLDS["correction_precision"]
        ),
        "out_of_scope_recall": (
            test_report["out_of_scope_recall"]
            >= _PRODUCTION_THRESHOLDS["out_of_scope_recall"]
        ),
        "selective_accuracy": (
            selective_accuracy is not None
            and selective_accuracy
            >= _PRODUCTION_THRESHOLDS["selective_accuracy"]
        ),
    }
    eligible = not sample_only
    return {
        "eligible": eligible,
        "passed": eligible and all(checks.values()),
        "reason": (
            "sample_only_evidence"
            if sample_only
            else (
                "thresholds_met"
                if all(checks.values())
                else "thresholds_not_met"
            )
        ),
        "thresholds": _PRODUCTION_THRESHOLDS,
        "checks": checks,
    }


def _manifest_without_model_checksum(
    *,
    dataset_sha256: str,
    seed: int,
    thresholds: dict[str, float],
    partitions: DatasetPartitions,
    sample_only: bool,
    model_id: str,
    model_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "model_id": model_id,
        "model_version": model_version,
        "adapter_type": "sklearn",
        "supported_task_types": list(_PUBLIC_TASK_LABELS),
        "normalization_version": NORMALIZATION_VERSION,
        "training_dataset_checksum": dataset_sha256,
        "library_versions": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
        "vectorizer": {
            "type": "TfidfVectorizer",
            "analyzer": "char",
            "ngram_range": [2, 5],
            "lowercase": False,
            "sublinear_tf": True,
        },
        "classifier": {
            "type": "LogisticRegression",
            "class_weight": "balanced",
            "max_iter": 2000,
            "random_state": seed,
        },
        "training_provenance": {
            "dataset_sha256": dataset_sha256,
            "seed": seed,
            "thresholds": thresholds,
            "sample_only": sample_only,
            "counts": {
                "train": len(partitions.train),
                "validation": len(partitions.validation),
                "test": len(partitions.test),
            },
        },
    }


def _publish(
    *,
    output: Path,
    pipeline: Pipeline,
    manifest_base: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    prefix = f".{output.name}.tmp-"
    temporary: Path | None = None
    try:
        temporary = Path(
            tempfile.mkdtemp(prefix=prefix, dir=output.parent)
        ).resolve(strict=True)
        if temporary.parent != output.parent or not temporary.name.startswith(
            prefix
        ):
            raise TrainingError("temporary artifact directory is unsafe")
        model_path = temporary / "model.joblib"
        joblib.dump(pipeline, model_path)
        _fsync_file(model_path)
        if model_path.stat().st_size > _MAX_MODEL_BYTES:
            raise TrainingError("trained model size exceeds limit")
        manifest = {
            **manifest_base,
            "model_artifact_checksum": _sha256_file(model_path),
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write_json(temporary / "manifest.json", manifest)
        _write_json(temporary / "metrics.json", metrics)
        _write_json(
            temporary / "label_map.json",
            {
                "model_labels": list(EXPECTED_LABELS),
                "public_task_types": list(_PUBLIC_TASK_LABELS),
                "out_of_scope_boundary": {
                    "label": None,
                    "status": "abstained",
                },
            },
        )
        _write_text(
            temporary / "dataset_checksum.txt",
            f"{manifest_base['training_dataset_checksum']}\n",
        )
        _fsync_directory(temporary)
        if os.path.lexists(output):
            raise TrainingError("output directory already exists")
        _atomic_rename(temporary, output)
        temporary = None
        _fsync_directory(output.parent)
    except TrainingError:
        raise
    except Exception:
        raise TrainingError("artifact publication failed") from None
    finally:
        if temporary is not None:
            _remove_temporary_directory(
                temporary,
                expected_parent=output.parent,
                expected_prefix=prefix,
            )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as destination:
        destination.write(encoded)
        destination.flush()
        os.fsync(destination.fileno())


def _write_text(path: Path, value: str) -> None:
    with path.open("xb") as destination:
        destination.write(value.encode("utf-8"))
        destination.flush()
        os.fsync(destination.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as artifact_file:
        artifact_file.flush()
        os.fsync(artifact_file.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_rename(source: Path, target: Path) -> None:
    if os.name == "nt":
        _windows_rename_no_replace(source, target)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source, target)
        return
    if sys.platform == "darwin":
        _macos_rename_no_replace(source, target)
        return
    raise OSError("atomic no-replace rename is unavailable")


def _windows_rename_no_replace(source: Path, target: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    move_file.restype = ctypes.c_int
    if not move_file(os.fspath(source), os.fspath(target)):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "atomic no-replace rename failed")


def _linux_rename_no_replace(source: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        rename_at_2 = library.renameat2
    except AttributeError:
        raise OSError("atomic no-replace rename is unavailable") from None
    rename_at_2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_at_2.restype = ctypes.c_int
    at_current_working_directory = -100
    rename_no_replace = 1
    result = rename_at_2(
        at_current_working_directory,
        os.fsencode(source),
        at_current_working_directory,
        os.fsencode(target),
        rename_no_replace,
    )
    if result != 0:
        error_code = ctypes.get_errno()
        raise OSError(error_code, "atomic no-replace rename failed")


def _macos_rename_no_replace(source: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    try:
        rename_exclusive = library.renamex_np
    except AttributeError:
        raise OSError("atomic no-replace rename is unavailable") from None
    rename_exclusive.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_exclusive.restype = ctypes.c_int
    rename_excl = 0x00000004
    result = rename_exclusive(
        os.fsencode(source),
        os.fsencode(target),
        rename_excl,
    )
    if result != 0:
        error_code = ctypes.get_errno()
        raise OSError(error_code, "atomic no-replace rename failed")


def _remove_temporary_directory(
    temporary: Path,
    *,
    expected_parent: Path,
    expected_prefix: str,
) -> None:
    try:
        resolved = temporary.resolve(strict=False)
        if (
            resolved.parent != expected_parent
            or not resolved.name.startswith(expected_prefix)
        ):
            return
        shutil.rmtree(resolved)
    except OSError:
        return


def _sha256_file(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise TrainingError("artifact input is unavailable") from None
    return digest.hexdigest()


def _is_sample_dataset(path: Path, *, dataset_sha256: str) -> bool:
    del path
    return dataset_sha256 in _SAMPLE_DATASET_SHA256S


def _validate_options(
    *,
    input_path: Path,
    output_dir: Path,
    runtime_dir: Path,
    model_id: str,
    model_version: str,
    seed: int,
    min_confidence: float,
    min_margin: float,
) -> None:
    if (
        not isinstance(input_path, Path)
        or not isinstance(output_dir, Path)
        or not isinstance(runtime_dir, Path)
    ):
        raise TrainingError("training paths are invalid")
    try:
        runtime_root = runtime_dir.resolve(strict=True)
        if not runtime_root.is_dir():
            raise TrainingError("runtime directory is unavailable")
    except TrainingError:
        raise
    except (OSError, RuntimeError):
        raise TrainingError("runtime directory is unavailable") from None
    if (
        not isinstance(model_id, str)
        or _SAFE_IDENTIFIER.fullmatch(model_id) is None
        or not isinstance(model_version, str)
        or len(model_version) > 56
        or _SAFE_IDENTIFIER.fullmatch(model_version) is None
    ):
        raise TrainingError("model identity is invalid")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TrainingError("training seed is invalid")
    for value in (min_confidence, min_margin):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise TrainingError("training thresholds are invalid")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train and publish an offline M4 intent model.",
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--min-confidence", required=True, type=float)
    parser.add_argument("--min-margin", required=True, type=float)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        train_and_publish(
            input_path=args.input,
            output_dir=args.output_dir,
            runtime_dir=args.runtime_dir,
            model_id=args.model_id,
            model_version=args.model_version,
            seed=args.seed,
            min_confidence=args.min_confidence,
            min_margin=args.min_margin,
        )
    except (IntentDatasetError, TrainingError) as error:
        print(f"intent training failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
