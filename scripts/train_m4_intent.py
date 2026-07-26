"""Train, evaluate, and atomically publish an offline M4 intent artifact."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_recall_fscore_support
from sklearn.pipeline import Pipeline

from course_insight.modules.m4_task_orchestration.intent_dataset import (
    EXPECTED_LABELS,
    DatasetPartitions,
    IntentDatasetError,
    IntentExample,
    load_dataset,
    split_by_group,
)


_SCHEMA_VERSION = 1
_ADAPTER_ID = "m4-sklearn-intent"
_MAX_MODEL_BYTES = 64 * 1024 * 1024
_SAMPLE_DATASET = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "m4_intent"
    / "example.jsonl"
)
_SAMPLE_DATASET_SHA256 = (
    "5f9651afc1e735831e4732bff0eae5b3"
    "c52ca8a55ed87260a4ea7ae221bf497a"
)


class TrainingError(RuntimeError):
    """A sanitized offline-training or artifact-publication failure."""


def train_and_publish(
    *,
    input_path: Path,
    output_dir: Path,
    seed: int,
    min_confidence: float,
    min_margin: float,
) -> dict[str, Any]:
    """Train once and atomically publish a Task 5-compatible artifact."""

    _validate_options(
        input_path=input_path,
        output_dir=output_dir,
        seed=seed,
        min_confidence=min_confidence,
        min_margin=min_margin,
    )
    output = output_dir.resolve(strict=False)
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
                    lowercase=True,
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
            [example.text for example in examples],
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
            [example.text for example in examples]
        )
    except Exception:
        raise TrainingError("intent model evaluation failed") from None

    predicted: list[str] = []
    covered: list[bool] = []
    for row in probability_rows:
        ranked = sorted(
            range(len(EXPECTED_LABELS)),
            key=row.__getitem__,
            reverse=True,
        )
        top_index, second_index = ranked[:2]
        confidence = float(row[top_index])
        margin = confidence - float(row[second_index])
        predicted.append(EXPECTED_LABELS[top_index])
        covered.append(
            confidence >= thresholds["min_confidence"]
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
    covered_count = sum(covered)
    selective_correct = sum(
        is_covered and is_correct
        for is_covered, is_correct in zip(covered, correct, strict=True)
    )
    label_counts = {
        label: truth.count(label)
        for label in EXPECTED_LABELS
    }
    return {
        "macro_f1": float(sum(f1) / len(EXPECTED_LABELS)),
        "per_class": per_class,
        "out_of_scope_recall": per_class["out_of_scope"]["recall"],
        "coverage": covered_count / len(examples),
        "selective_accuracy": (
            selective_correct / covered_count
            if covered_count
            else None
        ),
        "counts": {
            "examples": len(examples),
            "groups": group_count,
            "covered": covered_count,
            "selective_correct": selective_correct,
            "correct": sum(correct),
            "labels": label_counts,
        },
    }


def _manifest_without_model_checksum(
    *,
    dataset_sha256: str,
    seed: int,
    thresholds: dict[str, float],
    partitions: DatasetPartitions,
    sample_only: bool,
) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "adapter_id": _ADAPTER_ID,
        "adapter_version": f"1.0.{dataset_sha256[:12]}",
        "labels": list(EXPECTED_LABELS),
        "vectorizer": {
            "type": "TfidfVectorizer",
            "analyzer": "char",
            "ngram_range": [2, 5],
            "lowercase": True,
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
            "model_sha256": _sha256_file(model_path),
        }
        _write_json(temporary / "manifest.json", manifest)
        _write_json(temporary / "metrics.json", metrics)
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
    try:
        return (
            path.resolve(strict=True) == _SAMPLE_DATASET.resolve(strict=True)
            and dataset_sha256 == _SAMPLE_DATASET_SHA256
        )
    except (OSError, RuntimeError):
        return False


def _validate_options(
    *,
    input_path: Path,
    output_dir: Path,
    seed: int,
    min_confidence: float,
    min_margin: float,
) -> None:
    if not isinstance(input_path, Path) or not isinstance(output_dir, Path):
        raise TrainingError("training paths are invalid")
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
