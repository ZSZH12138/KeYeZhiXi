from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from course_insight.modules.m4_task_orchestration.intent_dataset import (
    EXPECTED_LABELS,
    IntentDatasetError,
    IntentExample,
    load_dataset,
    load_examples,
    split_by_group,
)


def _write_rows(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(
            f"{json.dumps(row, ensure_ascii=False, separators=(',', ':'))}\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    return path


def _explicit_rows() -> list[dict[str, object]]:
    return [
        {
            "text": f"{split}-{label}-示例",
            "label": label,
            "group_id": f"{split}-{label}",
            "split": split,
        }
        for split in ("train", "validation", "test")
        for label in EXPECTED_LABELS
    ]


def test_load_examples_accepts_exact_fields_and_optional_split(
    tmp_path: Path,
) -> None:
    path = _write_rows(
        tmp_path / "valid.jsonl",
        [
            {
                "text": "请解释这个概念",
                "label": "qa",
                "group_id": "qa-1",
                "split": "train",
            },
            {
                "text": "再给一道练习",
                "label": "practice",
                "group_id": "practice-1",
            },
        ],
    )

    examples = load_examples(path)

    assert examples == (
        IntentExample("请解释这个概念", "qa", "qa-1", "train"),
        IntentExample("再给一道练习", "practice", "practice-1", None),
    )


def test_load_dataset_hashes_the_exact_bytes_consumed(tmp_path: Path) -> None:
    path = _write_rows(
        tmp_path / "valid.jsonl",
        [
            {
                "text": "请解释这个概念",
                "label": "qa",
                "group_id": "qa-1",
            }
        ],
    )
    expected = sha256(path.read_bytes()).hexdigest()

    loaded = load_dataset(path)

    assert loaded.examples == (
        IntentExample("请解释这个概念", "qa", "qa-1", None),
    )
    assert loaded.sha256 == expected


@pytest.mark.parametrize(
    "payload",
    [
        b'{"text":"x","label":"qa","group_id":"g","extra":1}\n',
        b'{"text":"x","label":"qa"}\n',
        b'{"text":"","label":"qa","group_id":"g"}\n',
        b'{"text":"x","label":"qa","group_id":" "}\n',
        b'{"text":"x","label":"unknown","group_id":"g"}\n',
        b'{"text":"x","label":"qa","group_id":"g","split":""}\n',
        b'{"text":"x","label":"qa","group_id":"g","split":null}\n',
        b'{"text":"x","label":"qa","group_id":"g","split":"dev"}\n',
        b'{"text":"x","text":"private","label":"qa","group_id":"g"}\n',
        b'{"text":"x","label":"qa","group_id":"g","score":NaN}\n',
        b'{"text":"private","label":"qa","group_id":"g"\n',
        b"\xff\n",
        b"\n",
    ],
)
def test_load_examples_fails_closed_without_echoing_rows(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_bytes(payload)

    with pytest.raises(IntentDatasetError) as captured:
        load_examples(path)

    message = str(captured.value)
    assert "private" not in message
    decoded_row = payload.decode("utf-8", errors="ignore").strip()
    if decoded_row:
        assert decoded_row not in message
    assert captured.value.__cause__ is None


def test_load_examples_rejects_empty_and_oversized_inputs(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    oversized = tmp_path / "oversized.jsonl"
    oversized.write_bytes(b'{"text":"' + (b"x" * (64 * 1024)) + b'"}\n')

    with pytest.raises(IntentDatasetError, match="empty"):
        load_examples(empty)
    with pytest.raises(IntentDatasetError, match="size"):
        load_examples(oversized)


def test_load_examples_rejects_duplicate_rows_without_echoing_text(
    tmp_path: Path,
) -> None:
    private_text = "PRIVATE-LEARNER-TEXT"
    row = {
        "text": private_text,
        "label": "qa",
        "group_id": "qa-1",
    }
    path = _write_rows(tmp_path / "duplicate.jsonl", [row, row])

    with pytest.raises(IntentDatasetError, match="duplicate") as captured:
        load_examples(path)

    assert private_text not in str(captured.value)


def test_explicit_split_is_used_and_group_disjoint() -> None:
    examples = tuple(
        IntentExample(
            text=str(row["text"]),
            label=str(row["label"]),
            group_id=str(row["group_id"]),
            split=str(row["split"]),
        )
        for row in _explicit_rows()
    )

    partitions = split_by_group(examples, seed=20260726)

    assert len(partitions.train) == 6
    assert len(partitions.validation) == 6
    assert len(partitions.test) == 6
    assert set(partitions.train_groups).isdisjoint(partitions.validation_groups)
    assert set(partitions.train_groups).isdisjoint(partitions.test_groups)
    assert set(partitions.validation_groups).isdisjoint(partitions.test_groups)


def test_explicit_split_rejects_group_leakage() -> None:
    rows = _explicit_rows()
    rows[6] = {**rows[6], "group_id": str(rows[0]["group_id"])}
    examples = tuple(
        IntentExample(
            text=str(row["text"]),
            label=str(row["label"]),
            group_id=str(row["group_id"]),
            split=str(row["split"]),
        )
        for row in rows
    )

    with pytest.raises(IntentDatasetError, match="group"):
        split_by_group(examples, seed=7)


def test_split_rejects_mixed_explicit_and_automatic_rows() -> None:
    examples = (
        IntentExample("解释", "qa", "qa-1", "train"),
        IntentExample("练习", "practice", "practice-1", None),
    )

    with pytest.raises(IntentDatasetError, match="split"):
        split_by_group(examples, seed=7)


def test_automatic_split_is_deterministic_label_complete_and_disjoint() -> None:
    examples = tuple(
        IntentExample(
            text=f"{label}-{index}",
            label=label,
            group_id=f"{label}-group-{index}",
            split=None,
        )
        for label in EXPECTED_LABELS
        for index in range(3)
    )

    first = split_by_group(examples, seed=20260726)
    second = split_by_group(tuple(reversed(examples)), seed=20260726)

    assert first == second
    assert set(first.train_groups).isdisjoint(first.validation_groups)
    assert set(first.train_groups).isdisjoint(first.test_groups)
    assert set(first.validation_groups).isdisjoint(first.test_groups)
    for partition in (first.train, first.validation, first.test):
        assert {example.label for example in partition} == set(EXPECTED_LABELS)


def test_split_rejects_one_group_and_missing_required_labels() -> None:
    one_group = tuple(
        IntentExample(label, label, "only-group", None)
        for label in EXPECTED_LABELS
    )
    missing_label = tuple(
        IntentExample(
            text=f"{label}-{index}",
            label=label,
            group_id=f"{label}-{index}",
            split=None,
        )
        for label in EXPECTED_LABELS[:-1]
        for index in range(3)
    )

    with pytest.raises(IntentDatasetError, match="group"):
        split_by_group(one_group, seed=1)
    with pytest.raises(IntentDatasetError, match="labels"):
        split_by_group(missing_label, seed=1)


def test_explicit_split_requires_every_label_in_every_partition() -> None:
    rows = [
        row
        for row in _explicit_rows()
        if not (
            row["split"] == "test"
            and row["label"] == "stage_assessment"
        )
    ]
    examples = tuple(
        IntentExample(
            text=str(row["text"]),
            label=str(row["label"]),
            group_id=str(row["group_id"]),
            split=str(row["split"]),
        )
        for row in rows
    )

    with pytest.raises(IntentDatasetError, match="labels"):
        split_by_group(examples, seed=1)
