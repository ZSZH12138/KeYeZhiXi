from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from course_insight.modules.m4_task_orchestration.intent import (
    SUPPORTED_INTENT_LABELS,
    IntentStatus,
)
from course_insight.modules.m4_task_orchestration.intent_dataset import (
    EXPECTED_LABELS,
    IntentExample,
)
from course_insight.modules.m4_task_orchestration.normalization import (
    NORMALIZATION_VERSION,
)
from course_insight.modules.m4_task_orchestration.sklearn_adapter import (
    load_sklearn_intent_adapter,
)
from scripts import train_m4_intent


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "train_m4_intent.py"
pytestmark = pytest.mark.filterwarnings(
    "ignore:Setting the shape on a NumPy array has been deprecated:"
    "DeprecationWarning:joblib.numpy_pickle"
)


TEXTS = {
    "correction": ("修改答案", "订正错误", "纠正步骤"),
    "diagnostic": ("诊断问题", "检查薄弱点", "分析困难"),
    "out_of_scope": ("查询天气", "播放音乐", "推荐电影"),
    "practice": ("生成练习", "再做一题", "提供习题"),
    "qa": ("解释概念", "回答疑问", "说明含义"),
    "stage_assessment": ("阶段测评", "综合测试", "单元评估"),
}


def _write_dataset(path: Path) -> Path:
    rows = [
        {
            "example_id": f"{split}-{label}",
            "text": TEXTS[label][index],
            "label": label,
            "locale": "zh-CN",
            "paraphrase_group_id": f"{split}-{label}",
            "source": "manual",
            "approved": True,
            "notes": "",
            "split": split,
        }
        for index, split in enumerate(("train", "validation", "test"))
        for label in EXPECTED_LABELS
    ]
    path.write_text(
        "".join(
            f"{json.dumps(row, ensure_ascii=False, separators=(',', ':'))}\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return path


def _run_training(
    dataset: Path,
    output: Path,
    *,
    seed: int = 17,
    min_confidence: float = 0.0,
    min_margin: float = 0.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            os.fspath(SCRIPT),
            "--input",
            os.fspath(dataset),
            "--output-dir",
            os.fspath(output),
            "--runtime-dir",
            os.fspath(output.parent),
            "--model-id",
            "m4-intent-test",
            "--model-version",
            "1.0.0",
            "--seed",
            str(seed),
            "--min-confidence",
            str(min_confidence),
            "--min-margin",
            str(min_margin),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_training_cli_help_documents_governed_artifact_arguments() -> None:
    completed = subprocess.run(
        [sys.executable, os.fspath(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    for option in (
        "--input",
        "--runtime-dir",
        "--output-dir",
        "--model-id",
        "--model-version",
    ):
        assert option in completed.stdout


def _load_adapter(output: Path) -> Any:
    manifest = _read_json(output / "manifest.json")
    return load_sklearn_intent_adapter(
        output.resolve(),
        runtime_dir=output.parent.resolve(),
        expected_model_id=manifest["model_id"],
        expected_model_version=manifest["model_version"],
        expected_model_sha256=manifest["model_artifact_checksum"],
    )


def _example(label: str, index: int) -> IntentExample:
    return IntentExample(
        example_id=f"example-{index}",
        text=f"text-{index}",
        label=label,
        locale="zh-CN",
        paraphrase_group_id=f"group-{index}",
        source="manual",
        approved=True,
        notes="",
        split="test",
    )


def test_training_cli_is_reproducible_and_directly_loadable(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    first = tmp_path / "artifact-a"
    second = tmp_path / "artifact-b"

    first_run = _run_training(dataset, first, seed=17)
    second_run = _run_training(dataset, second, seed=17)

    assert first_run.returncode == 0, first_run.stderr
    assert second_run.returncode == 0, second_run.stderr
    assert _read_json(first / "metrics.json") == _read_json(
        second / "metrics.json"
    )
    adapter = _load_adapter(first)
    assert adapter.predict("请生成练习").status in {
        IntentStatus.ACCEPTED,
        IntentStatus.OUT_OF_SCOPE,
    }


def test_manifest_has_task5_schema_checksum_and_pipeline_metadata(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    output = tmp_path / "artifact"

    completed = _run_training(dataset, output)

    assert completed.returncode == 0, completed.stderr
    manifest = _read_json(output / "manifest.json")
    model_bytes = (output / "model.joblib").read_bytes()
    assert set(manifest) == {
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
    assert manifest["schema_version"] == "1"
    assert manifest["model_id"] == "m4-intent-test"
    assert manifest["model_version"] == "1.0.0"
    assert manifest["adapter_type"] == "sklearn"
    assert manifest["supported_task_types"] == [
        *SUPPORTED_INTENT_LABELS
    ]
    assert manifest["normalization_version"] == NORMALIZATION_VERSION
    assert manifest["model_artifact_checksum"] == sha256(model_bytes).hexdigest()
    assert manifest["training_dataset_checksum"] == sha256(
        dataset.read_bytes()
    ).hexdigest()
    assert set(manifest["library_versions"]) == {
        "python",
        "scikit_learn",
        "joblib",
    }
    assert manifest["vectorizer"] == {
        "type": "TfidfVectorizer",
        "analyzer": "char",
        "ngram_range": [2, 5],
        "lowercase": False,
        "sublinear_tf": True,
    }
    assert manifest["classifier"] == {
        "type": "LogisticRegression",
        "class_weight": "balanced",
        "max_iter": 2000,
        "random_state": 17,
    }
    _load_adapter(output)


def test_metrics_report_all_partitions_oos_groups_and_fixed_thresholds(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    output = tmp_path / "artifact"

    completed = _run_training(
        dataset,
        output,
        seed=31,
        min_confidence=0.7,
        min_margin=0.1,
    )

    assert completed.returncode == 0, completed.stderr
    metrics = _read_json(output / "metrics.json")
    assert metrics["seed"] == 31
    assert metrics["thresholds"] == {
        "min_confidence": 0.7,
        "min_margin": 0.1,
    }
    assert metrics["group_overlap"] == {
        "train_validation": [],
        "train_test": [],
        "validation_test": [],
        "has_overlap": False,
    }
    for split in ("train", "validation", "test"):
        report = metrics["partitions"][split]
        assert set(report) >= {
            "task_macro_f1",
            "per_class",
            "out_of_scope_recall",
            "task_coverage",
            "selective_accuracy",
            "confusion_matrix",
            "counts",
        }
        assert set(report["per_class"]) == set(EXPECTED_LABELS)
        assert report["confusion_matrix"]["labels"] == list(EXPECTED_LABELS)
        assert len(report["confusion_matrix"]["matrix"]) == 6
        assert all(
            len(row) == 6
            for row in report["confusion_matrix"]["matrix"]
        )
        assert report["counts"]["examples"] == 6
        assert report["counts"]["groups"] == 6
    assert set(metrics["production_gate"]) >= {
        "eligible",
        "passed",
        "checks",
        "thresholds",
    }


def test_thresholds_change_coverage_without_being_tuned(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    permissive = tmp_path / "permissive"
    strict = tmp_path / "strict"

    low = _run_training(
        dataset,
        permissive,
        min_confidence=0.0,
        min_margin=0.0,
    )
    high = _run_training(
        dataset,
        strict,
        min_confidence=1.0,
        min_margin=1.0,
    )

    assert low.returncode == 0, low.stderr
    assert high.returncode == 0, high.stderr
    low_metrics = _read_json(permissive / "metrics.json")
    high_metrics = _read_json(strict / "metrics.json")
    assert low_metrics["thresholds"]["min_confidence"] == 0.0
    assert high_metrics["thresholds"]["min_confidence"] == 1.0
    assert low_metrics["partitions"]["test"]["task_coverage"] == 1.0
    assert high_metrics["partitions"]["test"]["task_coverage"] == 0.0
    assert high_metrics["partitions"]["test"]["selective_accuracy"] is None


def test_existing_output_is_preserved_and_rejected(tmp_path: Path) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    output = tmp_path / "artifact"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    completed = _run_training(dataset, output)

    assert completed.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (output / "model.joblib").exists()


def test_interrupted_publication_removes_sibling_temp_and_no_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    output = tmp_path / "artifact"

    def fail_rename(source: Path, target: Path) -> None:
        del source, target
        raise OSError("private filesystem detail")

    monkeypatch.setattr(train_m4_intent, "_atomic_rename", fail_rename)

    with pytest.raises(train_m4_intent.TrainingError) as captured:
        train_m4_intent.train_and_publish(
            input_path=dataset,
            output_dir=output,
            runtime_dir=tmp_path,
            model_id="m4-intent-test",
            model_version="1.0.0",
            seed=17,
            min_confidence=0.7,
            min_margin=0.1,
        )

    assert "private filesystem detail" not in str(captured.value)
    assert not output.exists()
    assert list(tmp_path.glob(".artifact.tmp-*")) == []


def test_concurrent_empty_target_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    output = tmp_path / "artifact"
    original_atomic_rename = train_m4_intent._atomic_rename
    real_os_rename = os.rename

    def simulate_posix_replacement(source: Path, target: Path) -> None:
        target.rmdir()
        real_os_rename(source, target)

    def create_racing_target(source: Path, target: Path) -> None:
        target.mkdir()
        original_atomic_rename(source, target)

    monkeypatch.setattr(train_m4_intent.os, "rename", simulate_posix_replacement)
    monkeypatch.setattr(
        train_m4_intent,
        "_atomic_rename",
        create_racing_target,
    )

    with pytest.raises(train_m4_intent.TrainingError):
        train_m4_intent.train_and_publish(
            input_path=dataset,
            output_dir=output,
            runtime_dir=tmp_path,
            model_id="m4-intent-test",
            model_version="1.0.0",
            seed=17,
            min_confidence=0.7,
            min_margin=0.1,
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []
    assert list(tmp_path.glob(".artifact.tmp-*")) == []


def test_manifest_hash_remains_bound_to_loaded_dataset_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    original_bytes = dataset.read_bytes()
    output = tmp_path / "artifact"
    real_load_dataset = train_m4_intent.load_dataset

    def load_then_replace(path: Path) -> Any:
        loaded = real_load_dataset(path)
        path.write_bytes(original_bytes + b"\n")
        return loaded

    monkeypatch.setattr(train_m4_intent, "load_dataset", load_then_replace)

    train_m4_intent.train_and_publish(
        input_path=dataset,
        output_dir=output,
        runtime_dir=tmp_path,
        model_id="m4-intent-test",
        model_version="1.0.0",
        seed=17,
        min_confidence=0.7,
        min_margin=0.1,
    )

    manifest = _read_json(output / "manifest.json")
    assert manifest["training_dataset_checksum"] == sha256(
        original_bytes
    ).hexdigest()
    assert manifest["training_dataset_checksum"] != sha256(
        dataset.read_bytes()
    ).hexdigest()


def test_cli_rejects_invalid_threshold_without_creating_artifact(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(tmp_path / "dataset.jsonl")
    output = tmp_path / "artifact"

    completed = _run_training(dataset, output, min_confidence=1.1)

    assert completed.returncode != 0
    assert not output.exists()


def test_main_rejects_malformed_dataset_without_echoing_source_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_text = "PRIVATE-LEARNER-TEXT"
    dataset = tmp_path / "bad.jsonl"
    dataset.write_text(f'{{"text":"{private_text}"\n', encoding="utf-8")
    output = tmp_path / "artifact"

    exit_code = train_m4_intent.main(
        [
            "--input",
            os.fspath(dataset),
            "--output-dir",
            os.fspath(output),
            "--runtime-dir",
            os.fspath(tmp_path),
            "--model-id",
            "m4-intent-test",
            "--model-version",
            "1.0.0",
            "--seed",
            "17",
            "--min-confidence",
            "0.7",
            "--min-margin",
            "0.1",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert private_text not in captured.err
    assert not output.exists()


def test_repository_example_is_exact_neutral_sample_only_dataset(
    tmp_path: Path,
) -> None:
    source = ROOT / "data" / "m4_intent" / "example.jsonl"
    rows = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
    ]

    assert len(rows) == 18
    assert all(
        set(row)
        == {
            "example_id",
            "text",
            "label",
            "locale",
            "paraphrase_group_id",
            "source",
            "approved",
            "notes",
            "split",
        }
        for row in rows
    )
    assert {
        (row["split"], row["label"])
        for row in rows
    } == {
        (split, label)
        for split in ("train", "validation", "test")
        for label in EXPECTED_LABELS
    }
    output = tmp_path / "sample-artifact"
    completed = _run_training(source, output)
    assert completed.returncode == 0, completed.stderr
    metrics = _read_json(output / "metrics.json")
    assert metrics["evidence_scope"] == "sample-only"
    assert metrics["production_gate"]["eligible"] is False
    assert metrics["production_gate"]["passed"] is False


def test_sample_only_scope_requires_the_exact_example_checksum() -> None:
    source = ROOT / "data" / "m4_intent" / "example.jsonl"

    assert train_m4_intent._is_sample_dataset(
        source,
        dataset_sha256=sha256(source.read_bytes()).hexdigest(),
    )
    assert not train_m4_intent._is_sample_dataset(
        source,
        dataset_sha256="0" * 64,
    )
    crlf_checksum = sha256(
        source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    ).hexdigest()
    assert train_m4_intent._is_sample_dataset(
        Path("renamed.jsonl"),
        dataset_sha256=crlf_checksum,
    )


def test_copied_repository_sample_remains_sample_only(
    tmp_path: Path,
) -> None:
    source = ROOT / "data" / "m4_intent" / "example.jsonl"
    copied = tmp_path / "renamed-production-data.jsonl"
    copied.write_bytes(source.read_bytes())
    output = tmp_path / "artifact"

    completed = _run_training(copied, output)

    assert completed.returncode == 0, completed.stderr
    metrics = _read_json(output / "metrics.json")
    assert metrics["evidence_scope"] == "sample-only"
    assert metrics["production_gate"]["passed"] is False


def test_evaluation_uses_five_task_macro_and_excludes_oos_accepts() -> None:
    class _FakePipeline:
        def predict_proba(self, texts: list[str]) -> list[list[float]]:
            del texts
            # qa is deliberately predicted as OOS. The true OOS row is also OOS
            # and must not inflate task coverage.
            rows: list[list[float]] = []
            for index, label in enumerate(EXPECTED_LABELS):
                row = [0.01] * len(EXPECTED_LABELS)
                predicted = "out_of_scope" if label in {"qa", "out_of_scope"} else label
                row[EXPECTED_LABELS.index(predicted)] = 0.95
                rows.append(row)
            return rows

    examples = tuple(
        _example(label, index)
        for index, label in enumerate(EXPECTED_LABELS)
    )

    report = train_m4_intent._evaluate_partition(
        _FakePipeline(),
        examples,
        group_count=6,
        thresholds={"min_confidence": 0.7, "min_margin": 0.1},
    )

    task_f1 = [
        report["per_class"][label]["f1"]
        for label in EXPECTED_LABELS
        if label != "out_of_scope"
    ]
    all_f1 = [
        report["per_class"][label]["f1"]
        for label in EXPECTED_LABELS
    ]
    assert report["task_macro_f1"] == pytest.approx(sum(task_f1) / 5)
    assert report["task_macro_f1"] != pytest.approx(sum(all_f1) / 6)
    assert report["task_coverage"] == pytest.approx(4 / 5)
    assert report["counts"]["covered_in_scope"] == 4


def test_training_rejects_output_outside_runtime_boundary(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    dataset = _write_dataset(tmp_path / "dataset.jsonl")

    with pytest.raises(train_m4_intent.TrainingError, match="runtime"):
        train_m4_intent.train_and_publish(
            input_path=dataset,
            output_dir=tmp_path / "outside",
            runtime_dir=runtime,
            model_id="m4-intent-test",
            model_version="1.0.0",
            seed=17,
            min_confidence=0.7,
            min_margin=0.1,
        )
