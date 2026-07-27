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


def _row(
    *,
    example_id: str,
    text: str,
    label: str,
    group_id: str,
    split: str | None = None,
    approved: bool = True,
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "example_id": example_id,
        "text": text,
        "label": label,
        "locale": "zh-CN",
        "paraphrase_group_id": group_id,
        "source": "manual",
        "approved": approved,
        "notes": "",
    }
    if split is not None:
        row["split"] = split
    return {**row, **overrides}


def _example(
    *,
    example_id: str,
    text: str,
    label: str,
    group_id: str,
    split: str | None = None,
) -> IntentExample:
    return IntentExample(
        example_id=example_id,
        text=text,
        label=label,
        locale="zh-CN",
        paraphrase_group_id=group_id,
        source="manual",
        approved=True,
        notes="",
        split=split,
    )


def _explicit_rows() -> list[dict[str, object]]:
    return [
        _row(
            example_id=f"{split}-{label}",
            text=f"{split}-{label}-示例",
            label=label,
            group_id=f"{split}-{label}",
            split=split,
        )
        for split in ("train", "validation", "test")
        for label in EXPECTED_LABELS
    ]


def test_load_examples_accepts_exact_fields_and_optional_split(
    tmp_path: Path,
) -> None:
    path = _write_rows(
        tmp_path / "valid.jsonl",
        [
            _row(
                example_id="qa-001",
                text="请解释这个概念",
                label="qa",
                group_id="qa-1",
                split="train",
            ),
            _row(
                example_id="practice-001",
                text="再给一道练习",
                label="practice",
                group_id="practice-1",
            ),
        ],
    )

    examples = load_examples(path)

    assert examples == (
        _example(
            example_id="qa-001",
            text="请解释这个概念",
            label="qa",
            group_id="qa-1",
            split="train",
        ),
        _example(
            example_id="practice-001",
            text="再给一道练习",
            label="practice",
            group_id="practice-1",
        ),
    )


def test_load_dataset_hashes_the_exact_bytes_consumed(tmp_path: Path) -> None:
    path = _write_rows(
        tmp_path / "valid.jsonl",
        [
            _row(
                example_id="qa-001",
                text="请解释这个概念",
                label="qa",
                group_id="qa-1",
            )
        ],
    )
    expected = sha256(path.read_bytes()).hexdigest()

    loaded = load_dataset(path)

    assert loaded.examples == (
        _example(
            example_id="qa-001",
            text="请解释这个概念",
            label="qa",
            group_id="qa-1",
        ),
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
        **_row(
            example_id="qa-001",
            text=private_text,
            label="qa",
            group_id="qa-1",
        )
    }
    path = _write_rows(tmp_path / "duplicate.jsonl", [row, row])

    with pytest.raises(IntentDatasetError, match="duplicate") as captured:
        load_examples(path)

    assert private_text not in str(captured.value)


def test_load_examples_rejects_same_content_across_groups_and_splits(
    tmp_path: Path,
) -> None:
    private_text = "PRIVATE-DUPLICATE-CONTENT"
    path = _write_rows(
        tmp_path / "content-leak.jsonl",
        [
            _row(
                example_id="qa-train",
                text=private_text,
                label="qa",
                group_id="train-group",
                split="train",
            ),
            _row(
                example_id="qa-test",
                text=private_text,
                label="qa",
                group_id="test-group",
                split="test",
            ),
        ],
    )

    with pytest.raises(IntentDatasetError, match="duplicate") as captured:
        load_examples(path)

    assert private_text not in str(captured.value)


def test_load_examples_rejects_normalized_content_with_conflicting_labels(
    tmp_path: Path,
) -> None:
    path = _write_rows(
        tmp_path / "normalized-conflict.jsonl",
        [
            _row(
                example_id="normalized-1",
                text="Ａ  B",
                label="qa",
                group_id="qa-group",
            ),
            _row(
                example_id="normalized-2",
                text="a\tb",
                label="diagnostic",
                group_id="diagnostic-group",
            ),
        ],
    )

    with pytest.raises(IntentDatasetError, match="duplicate"):
        load_examples(path)


def test_explicit_split_is_used_and_group_disjoint() -> None:
    examples = tuple(
        IntentExample(
            example_id=str(row["example_id"]),
            text=str(row["text"]),
            label=str(row["label"]),
            locale=str(row["locale"]),
            paraphrase_group_id=str(row["paraphrase_group_id"]),
            source=str(row["source"]),
            approved=bool(row["approved"]),
            notes=str(row["notes"]),
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
    rows[6] = {
        **rows[6],
        "paraphrase_group_id": str(rows[0]["paraphrase_group_id"]),
    }
    examples = tuple(
        IntentExample(
            example_id=str(row["example_id"]),
            text=str(row["text"]),
            label=str(row["label"]),
            locale=str(row["locale"]),
            paraphrase_group_id=str(row["paraphrase_group_id"]),
            source=str(row["source"]),
            approved=bool(row["approved"]),
            notes=str(row["notes"]),
            split=str(row["split"]),
        )
        for row in rows
    )

    with pytest.raises(IntentDatasetError, match="group"):
        split_by_group(examples, seed=7)


def test_split_rejects_mixed_explicit_and_automatic_rows() -> None:
    examples = (
        _example(
            example_id="qa-1",
            text="解释",
            label="qa",
            group_id="qa-1",
            split="train",
        ),
        _example(
            example_id="practice-1",
            text="练习",
            label="practice",
            group_id="practice-1",
        ),
    )

    with pytest.raises(IntentDatasetError, match="split"):
        split_by_group(examples, seed=7)


def test_automatic_split_is_deterministic_label_complete_and_disjoint() -> None:
    examples = tuple(
        IntentExample(
            example_id=f"{label}-{index}",
            text=f"{label}-{index}",
            label=label,
            locale="zh-CN",
            paraphrase_group_id=f"{label}-group-{index}",
            source="manual",
            approved=True,
            notes="",
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


def test_automatic_split_finds_feasible_multilabel_group_assignment() -> None:
    labels_by_group = {
        "g0": {"correction", "out_of_scope", "qa"},
        "g1": {"diagnostic", "out_of_scope", "qa"},
        "g2": {"correction", "practice", "stage_assessment"},
        "g3": {"diagnostic", "out_of_scope", "practice"},
        "g4": {"correction", "stage_assessment"},
        "g5": {"correction", "qa", "stage_assessment"},
        "g6": {"diagnostic", "practice", "qa"},
    }
    examples = tuple(
        IntentExample(
            example_id=f"{group_id}-{label}",
            text=f"{group_id}-{label}",
            label=label,
            locale="zh-CN",
            paraphrase_group_id=group_id,
            source="manual",
            approved=True,
            notes="",
            split=None,
        )
        for group_id, labels in labels_by_group.items()
        for label in sorted(labels)
    )

    partitions = split_by_group(examples, seed=1)

    assert (
        set(partitions.train_groups)
        | set(partitions.validation_groups)
        | set(partitions.test_groups)
    ) == set(labels_by_group)
    assert set(partitions.train_groups).isdisjoint(partitions.validation_groups)
    assert set(partitions.train_groups).isdisjoint(partitions.test_groups)
    assert set(partitions.validation_groups).isdisjoint(partitions.test_groups)
    for partition in (partitions.train, partitions.validation, partitions.test):
        assert {example.label for example in partition} == set(EXPECTED_LABELS)


def test_automatic_split_includes_groups_beyond_signature_representatives() -> None:
    examples = tuple(
        IntentExample(
            example_id=f"{label}-{index}",
            text=f"{label}-{index}",
            label=label,
            locale="zh-CN",
            paraphrase_group_id=f"{label}-group-{index}",
            source="manual",
            approved=True,
            notes="",
            split=None,
        )
        for label in EXPECTED_LABELS
        for index in range(4)
    )

    partitions = split_by_group(examples, seed=17)

    all_groups = {
        example.group_id for example in examples
    }
    assigned_groups = (
        set(partitions.train_groups)
        | set(partitions.validation_groups)
        | set(partitions.test_groups)
    )
    assert assigned_groups == all_groups
    assert (
        len(partitions.train_groups)
        + len(partitions.validation_groups)
        + len(partitions.test_groups)
    ) == len(all_groups)


def test_split_rejects_one_group_and_missing_required_labels() -> None:
    one_group = tuple(
        _example(
            example_id=f"only-{label}",
            text=label,
            label=label,
            group_id="only-group",
        )
        for label in EXPECTED_LABELS
    )
    missing_label = tuple(
        IntentExample(
            example_id=f"{label}-{index}",
            text=f"{label}-{index}",
            label=label,
            locale="zh-CN",
            paraphrase_group_id=f"{label}-{index}",
            source="manual",
            approved=True,
            notes="",
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
            example_id=str(row["example_id"]),
            text=str(row["text"]),
            label=str(row["label"]),
            locale=str(row["locale"]),
            paraphrase_group_id=str(row["paraphrase_group_id"]),
            source=str(row["source"]),
            approved=bool(row["approved"]),
            notes=str(row["notes"]),
            split=str(row["split"]),
        )
        for row in rows
    )

    with pytest.raises(IntentDatasetError, match="labels"):
        split_by_group(examples, seed=1)


def test_load_examples_rejects_duplicate_ids_and_unapproved_rows(
    tmp_path: Path,
) -> None:
    duplicate_id = _write_rows(
        tmp_path / "duplicate-id.jsonl",
        [
            _row(
                example_id="duplicate",
                text="解释概念",
                label="qa",
                group_id="qa-1",
            ),
            _row(
                example_id="duplicate",
                text="再来一道题",
                label="practice",
                group_id="practice-1",
            ),
        ],
    )
    unapproved = _write_rows(
        tmp_path / "unapproved.jsonl",
        [
            _row(
                example_id="unapproved",
                text="未批准样本",
                label="qa",
                group_id="qa-2",
                approved=False,
            )
        ],
    )

    with pytest.raises(IntentDatasetError, match="duplicate"):
        load_examples(duplicate_id)
    with pytest.raises(IntentDatasetError, match="approved"):
        load_examples(unapproved)


@pytest.mark.parametrize(
    "overrides",
    [
        {"example_id": "bad id"},
        {"locale": "../private"},
        {"paraphrase_group_id": " "},
        {"source": "http://private"},
        {"approved": "true"},
        {"notes": 1},
    ],
)
def test_load_examples_rejects_invalid_governance_metadata(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    path = _write_rows(
        tmp_path / "invalid-governance.jsonl",
        [
            {
                **_row(
                    example_id="qa-001",
                    text="解释概念",
                    label="qa",
                    group_id="qa-1",
                ),
                **overrides,
            }
        ],
    )

    with pytest.raises(IntentDatasetError):
        load_examples(path)
