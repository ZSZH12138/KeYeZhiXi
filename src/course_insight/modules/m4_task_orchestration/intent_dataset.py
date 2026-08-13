"""Strict, privacy-safe JSONL loading and deterministic intent partitions."""

from __future__ import annotations

import json
import os
import random
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from course_insight.modules.m4_task_orchestration.normalization import (
    normalize_intent_text,
)


EXPECTED_LABELS = (
    "correction",
    "diagnostic",
    "out_of_scope",
    "practice",
    "qa",
    "stage_assessment",
)
EXPECTED_SPLITS = ("train", "validation", "test")
_BASE_FIELDS = frozenset(
    {
        "example_id",
        "text",
        "label",
        "locale",
        "paraphrase_group_id",
        "source",
        "approved",
        "notes",
    }
)
_ALL_FIELDS = frozenset({*_BASE_FIELDS, "split"})
_MAX_ROW_BYTES = 64 * 1024
_MAX_EXAMPLES = 1_000_000
_MAX_NOTES_LENGTH = 1024
_SAFE_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LABEL_BITS = {
    label: 1 << index for index, label in enumerate(EXPECTED_LABELS)
}
_FULL_LABEL_MASK = (1 << len(EXPECTED_LABELS)) - 1
_PARTITION_COUNT = 3


class IntentDatasetError(ValueError):
    """A sanitized failure to validate or partition an intent dataset."""


@dataclass(frozen=True, slots=True)
class IntentExample:
    """One immutable intent example; source text is never used in errors."""

    example_id: str
    text: str
    label: str
    locale: str
    paraphrase_group_id: str
    source: str
    approved: bool
    notes: str
    split: str | None

    @property
    def group_id(self) -> str:
        """Compatibility alias for deterministic partition internals."""

        return self.paraphrase_group_id


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
    seen_example_ids: set[str] = set()
    seen_content: set[str] = set()
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
                if example.example_id in seen_example_ids:
                    raise IntentDatasetError(
                        "dataset contains duplicate example ids"
                    )
                seen_example_ids.add(example.example_id)
                content_key = _content_identity(example.text)
                if content_key in seen_content:
                    raise IntentDatasetError(
                        "dataset contains duplicate content"
                    )
                seen_content.add(content_key)
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
    example_id = _safe_metadata(raw["example_id"])
    row_text = _nonblank_string(raw["text"])
    label = _nonblank_string(raw["label"])
    locale = _safe_metadata(raw["locale"])
    group_id = _safe_metadata(raw["paraphrase_group_id"])
    source = _safe_metadata(raw["source"])
    approved = raw["approved"]
    notes = raw["notes"]
    if label not in EXPECTED_LABELS:
        raise IntentDatasetError("dataset label is invalid")
    if approved is not True:
        raise IntentDatasetError("dataset row must be explicitly approved")
    if not isinstance(notes, str) or len(notes) > _MAX_NOTES_LENGTH:
        raise IntentDatasetError("dataset row values are invalid")
    if "split" in raw:
        split_raw = raw["split"]
        split = _nonblank_string(split_raw)
        if split not in EXPECTED_SPLITS:
            raise IntentDatasetError("dataset split is invalid")
    else:
        split = None
    return IntentExample(
        example_id=example_id,
        text=row_text,
        label=label,
        locale=locale,
        paraphrase_group_id=group_id,
        source=source,
        approved=True,
        notes=notes,
        split=split,
    )


def _nonblank_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntentDatasetError("dataset row values are invalid")
    return value


def _safe_metadata(value: object) -> str:
    if not _is_safe_metadata(value):
        raise IntentDatasetError("dataset governance metadata is invalid")
    assert isinstance(value, str)
    return value


def _is_safe_metadata(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_METADATA.fullmatch(value) is not None
    )


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
    seen_example_ids: set[str] = set()
    seen_content: set[str] = set()
    for example in normalized:
        if (
            not _is_safe_metadata(example.example_id)
            or not isinstance(example.text, str)
            or not example.text.strip()
            or example.label not in EXPECTED_LABELS
            or not _is_safe_metadata(example.locale)
            or not _is_safe_metadata(example.paraphrase_group_id)
            or not _is_safe_metadata(example.source)
            or example.approved is not True
            or not isinstance(example.notes, str)
            or len(example.notes) > _MAX_NOTES_LENGTH
            or example.split not in {*EXPECTED_SPLITS, None}
        ):
            raise IntentDatasetError("dataset examples are invalid")
        if example.example_id in seen_example_ids:
            raise IntentDatasetError("dataset contains duplicate example ids")
        seen_example_ids.add(example.example_id)
        content_key = _content_identity(example.text)
        if content_key in seen_content:
            raise IntentDatasetError("dataset contains duplicate content")
        seen_content.add(content_key)
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.paraphrase_group_id,
                item.label,
                item.text,
                item.example_id,
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

    by_signature: dict[int, list[str]] = defaultdict(list)
    for group_id, group_examples in grouped.items():
        signature = 0
        for example in group_examples:
            signature |= _LABEL_BITS[example.label]
        by_signature[signature].append(group_id)

    representatives: list[tuple[str, int]] = []
    for signature in sorted(by_signature):
        group_ids = sorted(by_signature[signature])
        random.Random(f"{seed}:signature:{signature}").shuffle(group_ids)
        representatives.extend(
            (group_id, signature)
            for group_id in group_ids[:_PARTITION_COUNT]
        )
    random.Random(f"{seed}:search").shuffle(representatives)
    assigned = _search_complete_assignment(representatives, seed=seed)
    if assigned is None:
        raise IntentDatasetError(
            "dataset groups cannot provide all labels in every partition"
        )
    _assign_remaining_groups(
        assigned,
        all_group_ids=tuple(grouped),
        seed=seed,
    )
    return _partitions_from_group_ids(examples, assigned)


def _search_complete_assignment(
    representatives: list[tuple[str, int]],
    *,
    seed: int,
) -> list[list[str]] | None:
    """Find three disjoint covers over a fixed 64^3 state space.

    At most three groups per signature are required: a coverage solution never
    needs more than one identical signature in the same partition. A state is
    the six-bit label mask reached by each partition. Skipping is explicit, so
    all feasible representative assignments are explored without a retry cap.
    """

    empty_state = (0, 0, 0)
    goal = (_FULL_LABEL_MASK,) * _PARTITION_COUNT
    states: dict[tuple[int, int, int], int] = {empty_state: 0}
    goal_code: int | None = None
    for index, (group_id, signature) in enumerate(representatives):
        next_states = dict(states)
        partition_order = list(range(_PARTITION_COUNT))
        random.Random(f"{seed}:partition:{group_id}").shuffle(partition_order)
        choice_shift = index * 2
        for state, choice_code in states.items():
            for partition_index in partition_order:
                updated_mask = state[partition_index] | signature
                if updated_mask == state[partition_index]:
                    continue
                updated_state = list(state)
                updated_state[partition_index] = updated_mask
                state_key = tuple(updated_state)
                if state_key not in next_states:
                    next_states[state_key] = choice_code | (
                        (partition_index + 1) << choice_shift
                    )
        states = next_states
        if goal in states:
            goal_code = states[goal]
            break
    if goal_code is None:
        return None

    assigned: list[list[str]] = [
        [] for _ in range(_PARTITION_COUNT)
    ]
    for index, (group_id, _) in enumerate(representatives):
        choice = (goal_code >> (index * 2)) & 0b11
        if choice:
            assigned[choice - 1].append(group_id)
    return assigned


def _assign_remaining_groups(
    assigned: list[list[str]],
    *,
    all_group_ids: tuple[str, ...],
    seed: int,
) -> None:
    already_assigned = {
        group_id
        for partition in assigned
        for group_id in partition
    }
    remaining = sorted(set(all_group_ids) - already_assigned)
    random.Random(f"{seed}:remaining").shuffle(remaining)
    for group_id in remaining:
        partition_order = list(range(_PARTITION_COUNT))
        random.Random(f"{seed}:balance:{group_id}").shuffle(partition_order)
        partition_index = min(
            partition_order,
            key=lambda index: len(assigned[index]),
        )
        assigned[partition_index].append(group_id)


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


def _content_identity(text: str) -> str:
    try:
        return normalize_intent_text(text)
    except ValueError:
        raise IntentDatasetError("dataset row values are invalid") from None


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
