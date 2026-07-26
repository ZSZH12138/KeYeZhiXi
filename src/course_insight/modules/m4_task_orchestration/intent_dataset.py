"""Strict, privacy-safe JSONL loading and deterministic intent partitions."""

from __future__ import annotations

import json
import os
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


EXPECTED_LABELS = (
    "correction",
    "diagnostic",
    "out_of_scope",
    "practice",
    "qa",
    "stage_assessment",
)
EXPECTED_SPLITS = ("train", "validation", "test")
_BASE_FIELDS = frozenset({"text", "label", "group_id"})
_ALL_FIELDS = frozenset({*_BASE_FIELDS, "split"})
_MAX_ROW_BYTES = 64 * 1024
_MAX_EXAMPLES = 1_000_000


class IntentDatasetError(ValueError):
    """A sanitized failure to validate or partition an intent dataset."""


@dataclass(frozen=True, slots=True)
class IntentExample:
    """One immutable intent example; source text is never used in errors."""

    text: str
    label: str
    group_id: str
    split: str | None


@dataclass(frozen=True, slots=True)
class LoadedIntentDataset:
    """Examples and the SHA-256 of the exact stable byte snapshot parsed."""

    examples: tuple[IntentExample, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class DatasetPartitions:
    """Immutable examples and group identities for three disjoint splits."""

    train: tuple[IntentExample, ...]
    validation: tuple[IntentExample, ...]
    test: tuple[IntentExample, ...]
    train_groups: tuple[str, ...]
    validation_groups: tuple[str, ...]
    test_groups: tuple[str, ...]


def load_examples(path: Path) -> tuple[IntentExample, ...]:
    """Load bounded UTF-8 JSONL with exact fields and duplicate rejection."""

    return load_dataset(path).examples


def load_dataset(path: Path) -> LoadedIntentDataset:
    """Load examples and hash the same stable file snapshot in one pass."""

    if not isinstance(path, Path):
        raise IntentDatasetError("dataset path is invalid")
    examples: list[IntentExample] = []
    seen: set[IntentExample] = set()
    digest = sha256()
    byte_count = 0
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            line_number = 0
            while True:
                payload = source.readline(_MAX_ROW_BYTES + 2)
                if not payload:
                    break
                digest.update(payload)
                byte_count += len(payload)
                line_number += 1
                if len(payload) > _MAX_ROW_BYTES:
                    raise IntentDatasetError("dataset row size exceeds limit")
                example = _parse_line(payload, line_number=line_number)
                if example in seen:
                    raise IntentDatasetError("dataset contains duplicate rows")
                seen.add(example)
                examples.append(example)
                if len(examples) > _MAX_EXAMPLES:
                    raise IntentDatasetError("dataset row count exceeds limit")
            after = os.fstat(source.fileno())
    except IntentDatasetError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise IntentDatasetError("dataset is unavailable") from None
    if not examples:
        raise IntentDatasetError("dataset is empty")
    if (
        _snapshot_identity(before) != _snapshot_identity(after)
        or byte_count != after.st_size
    ):
        raise IntentDatasetError("dataset changed during validation")
    return LoadedIntentDataset(
        examples=tuple(examples),
        sha256=digest.hexdigest(),
    )


def split_by_group(
    examples: Sequence[IntentExample],
    *,
    seed: int,
) -> DatasetPartitions:
    """Use all explicit splits or deterministically create group-only splits."""

    normalized = _validated_examples(examples)
    split_flags = {example.split is not None for example in normalized}
    if len(split_flags) != 1:
        raise IntentDatasetError("dataset split declarations are inconsistent")
    if split_flags == {True}:
        return _explicit_partitions(normalized)
    return _automatic_partitions(normalized, seed=seed)


def _parse_line(payload: bytes, *, line_number: int) -> IntentExample:
    del line_number
    if not payload.endswith(b"\n") and len(payload) >= _MAX_ROW_BYTES:
        raise IntentDatasetError("dataset row size exceeds limit")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise IntentDatasetError("dataset must be UTF-8") from None
    if not text.strip():
        raise IntentDatasetError("dataset contains a blank row")
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateKey:
        raise IntentDatasetError("dataset row contains duplicate fields") from None
    except (json.JSONDecodeError, ValueError):
        raise IntentDatasetError("dataset row is not valid JSON") from None
    if not isinstance(raw, dict) or frozenset(raw) not in {
        _BASE_FIELDS,
        _ALL_FIELDS,
    }:
        raise IntentDatasetError("dataset row fields are invalid")
    row_text = _nonblank_string(raw["text"])
    label = _nonblank_string(raw["label"])
    group_id = _nonblank_string(raw["group_id"])
    if label not in EXPECTED_LABELS:
        raise IntentDatasetError("dataset label is invalid")
    if "split" in raw:
        split_raw = raw["split"]
        split = _nonblank_string(split_raw)
        if split not in EXPECTED_SPLITS:
            raise IntentDatasetError("dataset split is invalid")
    else:
        split = None
    return IntentExample(
        text=row_text,
        label=label,
        group_id=group_id,
        split=split,
    )


def _nonblank_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntentDatasetError("dataset row values are invalid")
    return value


def _validated_examples(
    examples: Sequence[IntentExample],
) -> tuple[IntentExample, ...]:
    if (
        isinstance(examples, (str, bytes))
        or not isinstance(examples, Sequence)
        or not examples
    ):
        raise IntentDatasetError("dataset examples are invalid")
    normalized = tuple(examples)
    if any(not isinstance(example, IntentExample) for example in normalized):
        raise IntentDatasetError("dataset examples are invalid")
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.group_id,
                item.label,
                item.text,
                "" if item.split is None else item.split,
            ),
        )
    )


def _explicit_partitions(
    examples: tuple[IntentExample, ...],
) -> DatasetPartitions:
    group_splits: dict[str, str] = {}
    buckets: dict[str, list[IntentExample]] = {
        split: [] for split in EXPECTED_SPLITS
    }
    for example in examples:
        assert example.split is not None
        existing = group_splits.setdefault(example.group_id, example.split)
        if existing != example.split:
            raise IntentDatasetError("dataset group crosses split partitions")
        buckets[example.split].append(example)
    return _build_partitions(
        tuple(buckets["train"]),
        tuple(buckets["validation"]),
        tuple(buckets["test"]),
    )


def _automatic_partitions(
    examples: tuple[IntentExample, ...],
    *,
    seed: int,
) -> DatasetPartitions:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise IntentDatasetError("dataset split seed is invalid")
    grouped: dict[str, list[IntentExample]] = defaultdict(list)
    for example in examples:
        grouped[example.group_id].append(example)
    if len(grouped) < 3:
        raise IntentDatasetError("dataset needs at least three groups")

    by_signature: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for group_id, group_examples in grouped.items():
        signature = tuple(sorted({example.label for example in group_examples}))
        by_signature[signature].append(group_id)

    for attempt in range(256):
        assigned: list[list[str]] = [[], [], []]
        for signature in sorted(by_signature):
            group_ids = sorted(by_signature[signature])
            random.Random(f"{seed}:{attempt}:{'|'.join(signature)}").shuffle(
                group_ids
            )
            offset = random.Random(
                f"{seed}:{attempt}:offset:{'|'.join(signature)}"
            ).randrange(3)
            for index, group_id in enumerate(group_ids):
                assigned[(index + offset) % 3].append(group_id)
        candidate = _partitions_from_group_ids(examples, assigned)
        if _partitions_have_all_labels(candidate):
            return candidate
    raise IntentDatasetError(
        "dataset groups cannot provide all labels in every partition"
    )


def _partitions_from_group_ids(
    examples: tuple[IntentExample, ...],
    assigned: list[list[str]],
) -> DatasetPartitions:
    group_indexes = [
        {group_id for group_id in partition_groups}
        for partition_groups in assigned
    ]
    partition_examples = [
        tuple(
            example
            for example in examples
            if example.group_id in group_ids
        )
        for group_ids in group_indexes
    ]
    return _build_partitions(*partition_examples, validate_labels=False)


def _build_partitions(
    train: tuple[IntentExample, ...],
    validation: tuple[IntentExample, ...],
    test: tuple[IntentExample, ...],
    *,
    validate_labels: bool = True,
) -> DatasetPartitions:
    partitions = DatasetPartitions(
        train=train,
        validation=validation,
        test=test,
        train_groups=tuple(sorted({item.group_id for item in train})),
        validation_groups=tuple(
            sorted({item.group_id for item in validation})
        ),
        test_groups=tuple(sorted({item.group_id for item in test})),
    )
    _require_group_disjointness(partitions)
    if validate_labels and not _partitions_have_all_labels(partitions):
        raise IntentDatasetError(
            "dataset partitions must each contain all expected labels"
        )
    return partitions


def _partitions_have_all_labels(partitions: DatasetPartitions) -> bool:
    expected = set(EXPECTED_LABELS)
    return all(
        {example.label for example in partition} == expected
        for partition in (
            partitions.train,
            partitions.validation,
            partitions.test,
        )
    )


def _require_group_disjointness(partitions: DatasetPartitions) -> None:
    train = set(partitions.train_groups)
    validation = set(partitions.validation_groups)
    test = set(partitions.test_groups)
    if (
        train & validation
        or train & test
        or validation & test
    ):
        raise IntentDatasetError("dataset group crosses split partitions")


def _snapshot_identity(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


class _DuplicateKey(ValueError):
    pass


def _unique_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("invalid JSON constant")
