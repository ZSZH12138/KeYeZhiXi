"""Teacher-bank-only item selection policies for the four student modes."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from course_insight.contracts.errors import DomainError


AssessmentMode = Literal[
    "diagnostic", "practice", "correction", "stage_assessment"
]


@dataclass(frozen=True, slots=True)
class ConceptMastery:
    attempted_count: int
    correct_count: int

    def __post_init__(self) -> None:
        if self.attempted_count < 0 or self.correct_count < 0:
            raise ValueError("mastery counts must not be negative")
        if self.correct_count > self.attempted_count:
            raise ValueError("correct count must not exceed attempted count")

    @property
    def attempt_status(self) -> Literal["unseen", "attempted"]:
        return "unseen" if self.attempted_count == 0 else "attempted"

    @property
    def mastery(self) -> float:
        if self.attempted_count == 0:
            return 0.0
        return min(self.correct_count / self.attempted_count, 0.9)


@dataclass(frozen=True, slots=True)
class SelectionItem:
    item_id: str
    concept_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.item_id or not self.concept_ids:
            raise ValueError("selection items require an id and concepts")


@dataclass(frozen=True, slots=True)
class AssessmentSelectionContext:
    mastery_by_concept: Mapping[str, ConceptMastery]
    open_wrong_item_ids: tuple[str, ...]


def select_items(
    mode: AssessmentMode,
    items: Sequence[SelectionItem],
    *,
    mastery_by_concept: Mapping[str, ConceptMastery],
    open_wrong_item_ids: Sequence[str],
    rng: random.Random,
) -> tuple[str, ...]:
    """Select immutable item IDs without generating or altering teacher items."""

    if not items:
        raise DomainError(
            code="TEACHER_QUESTION_BANK_EMPTY",
            module="m8",
            message="教师尚未上传并发布可用题目，试卷生成失败。",
            recoverable=True,
        )
    if mode == "diagnostic":
        return _diagnostic(items, rng)
    if mode == "practice":
        return _practice(items, mastery_by_concept, rng)
    if mode == "stage_assessment":
        return _stage(items, mastery_by_concept)
    if mode == "correction":
        return _correction(items, open_wrong_item_ids)
    raise ValueError(f"unsupported assessment mode: {mode}")


def _diagnostic(
    items: Sequence[SelectionItem], rng: random.Random
) -> tuple[str, ...]:
    if len(items) <= 20:
        return tuple(item.item_id for item in items)
    return tuple(item.item_id for item in rng.sample(list(items), 20))


def _practice(
    items: Sequence[SelectionItem],
    mastery: Mapping[str, ConceptMastery],
    rng: random.Random,
) -> tuple[str, ...]:
    selected: list[str] = []
    for item in items:
        records = tuple(mastery.get(concept_id) for concept_id in item.concept_ids)
        if any(record is None or record.attempt_status == "unseen" for record in records):
            continue
        weakest = min(record.mastery for record in records if record is not None)
        if rng.random() < 1.0 - weakest:
            selected.append(item.item_id)
    if not selected:
        raise DomainError(
            code="NO_PRACTICE_ITEMS_SELECTED",
            module="m8",
            message="当前没有命中可练习题目，请重试或先完成诊断测评。",
            recoverable=True,
        )
    return tuple(selected)


def _stage(
    items: Sequence[SelectionItem],
    mastery: Mapping[str, ConceptMastery],
) -> tuple[str, ...]:
    target = min(20, len(items))
    if target == len(items):
        return tuple(item.item_id for item in items)

    grouped: dict[str, list[SelectionItem]] = {}
    for item in items:
        owner = min(
            item.concept_ids,
            key=lambda concept_id: (_mastery(concept_id, mastery), concept_id),
        )
        grouped = {**grouped, owner: [*grouped.get(owner, ()), item]}

    selected: list[str] = []
    available = {concept_id: list(group) for concept_id, group in grouped.items()}
    while len(selected) < target and available:
        remaining = target - len(selected)
        weights = {
            concept_id: 1.0 - _mastery(concept_id, mastery)
            for concept_id in available
        }
        quotas = _largest_remainder(weights, remaining)
        taken = 0
        next_available: dict[str, list[SelectionItem]] = {}
        for concept_id in sorted(
            available,
            key=lambda value: (_mastery(value, mastery), value),
        ):
            group = available[concept_id]
            count = min(quotas.get(concept_id, 0), len(group))
            selected.extend(item.item_id for item in group[:count])
            taken += count
            if group[count:]:
                next_available[concept_id] = group[count:]
        if taken == 0:
            # This can only occur through numeric edge cases; choose the weakest
            # available group to guarantee forward progress.
            concept_id = min(
                available,
                key=lambda value: (_mastery(value, mastery), value),
            )
            selected.append(available[concept_id][0].item_id)
            remainder = available[concept_id][1:]
            next_available = {
                **{key: value for key, value in available.items() if key != concept_id},
                **({concept_id: remainder} if remainder else {}),
            }
        available = next_available
    return tuple(selected[:target])


def _correction(
    items: Sequence[SelectionItem], open_wrong_item_ids: Sequence[str]
) -> tuple[str, ...]:
    open_ids = frozenset(open_wrong_item_ids)
    selected = tuple(item.item_id for item in items if item.item_id in open_ids)
    if not selected:
        raise DomainError(
            code="NO_OPEN_WRONG_QUESTIONS",
            module="m8",
            message="当前没有需要订正的错题。",
            recoverable=True,
        )
    return selected


def _mastery(
    concept_id: str, mastery: Mapping[str, ConceptMastery]
) -> float:
    record = mastery.get(concept_id)
    return 0.0 if record is None else record.mastery


def _largest_remainder(
    weights: Mapping[str, float], item_count: int
) -> dict[str, int]:
    if item_count <= 0 or not weights:
        return {}
    total = math.fsum(weights.values())
    raw = {
        concept_id: item_count * weight / total
        for concept_id, weight in weights.items()
    }
    quotas = {concept_id: math.floor(value) for concept_id, value in raw.items()}
    remainder = item_count - sum(quotas.values())
    ordered = sorted(
        raw,
        key=lambda concept_id: (
            -(raw[concept_id] - quotas[concept_id]),
            concept_id,
        ),
    )
    return {
        concept_id: quotas[concept_id] + int(concept_id in ordered[:remainder])
        for concept_id in quotas
    }


__all__ = [
    "AssessmentMode",
    "AssessmentSelectionContext",
    "ConceptMastery",
    "SelectionItem",
    "select_items",
]
