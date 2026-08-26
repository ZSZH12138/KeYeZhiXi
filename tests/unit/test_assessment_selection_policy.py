from __future__ import annotations

import random

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m8_assessment_scoring.selection_policy import (
    AssessmentSelectionContext,
    ConceptMastery,
    SelectionItem,
    select_items,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from tests.factories.m5_m8 import make_knowledge_bundle, make_task_plan


def _items(count: int, concept_id: str = "A") -> tuple[SelectionItem, ...]:
    return tuple(
        SelectionItem(item_id=f"q{index:02d}", concept_ids=(concept_id,))
        for index in range(1, count + 1)
    )


def test_diagnostic_randomly_draws_twenty_or_all_teacher_items() -> None:
    items = _items(25)
    selected = select_items(
        "diagnostic",
        items,
        mastery_by_concept={},
        open_wrong_item_ids=(),
        rng=random.Random(7),
    )
    assert len(selected) == 20
    assert len(set(selected)) == 20
    assert set(selected) <= {item.item_id for item in items}
    assert select_items(
        "diagnostic",
        _items(7),
        mastery_by_concept={},
        open_wrong_item_ids=(),
        rng=random.Random(7),
    ) == tuple(item.item_id for item in _items(7))


def test_practice_excludes_unseen_concepts_and_uses_weakest_probability() -> None:
    items = (
        SelectionItem(item_id="weak", concept_ids=("A",)),
        SelectionItem(item_id="mixed", concept_ids=("A", "B")),
        SelectionItem(item_id="unseen", concept_ids=("C",)),
    )
    mastery = {
        "A": ConceptMastery(attempted_count=1, correct_count=0),
        "B": ConceptMastery(attempted_count=10, correct_count=9),
        "C": ConceptMastery(attempted_count=0, correct_count=0),
    }
    selected = select_items(
        "practice",
        items,
        mastery_by_concept=mastery,
        open_wrong_item_ids=(),
        rng=random.Random(1),
    )
    assert selected == ("weak", "mixed")
    assert mastery["B"].mastery == 0.9
    assert mastery["C"].attempt_status == "unseen"


def test_stage_uses_weakness_quota_and_redistributes_shortages() -> None:
    items = (
        *_items(2, "A"),
        *(SelectionItem(item_id=f"b{i}", concept_ids=("B",)) for i in range(8)),
        *(SelectionItem(item_id=f"c{i}", concept_ids=("C",)) for i in range(8)),
        *(SelectionItem(item_id=f"d{i}", concept_ids=("D",)) for i in range(8)),
    )
    mastery = {
        "A": ConceptMastery(10, 1),
        "B": ConceptMastery(10, 2),
        "C": ConceptMastery(10, 3),
        "D": ConceptMastery(10, 4),
    }
    selected = select_items(
        "stage_assessment",
        items,
        mastery_by_concept=mastery,
        open_wrong_item_ids=(),
        rng=random.Random(0),
    )
    assert len(selected) == 20
    assert len([item_id for item_id in selected if item_id.startswith("q")]) == 2
    assert len(set(selected)) == 20


def test_stage_literal_example_allocates_six_five_five_four() -> None:
    items = tuple(
        SelectionItem(item_id=f"{concept}{index}", concept_ids=(concept,))
        for concept in "ABCD"
        for index in range(10)
    )
    selected = select_items(
        "stage_assessment",
        items,
        mastery_by_concept={
            "A": ConceptMastery(10, 1),
            "B": ConceptMastery(10, 2),
            "C": ConceptMastery(10, 3),
            "D": ConceptMastery(10, 4),
        },
        open_wrong_item_ids=(),
        rng=random.Random(0),
    )
    assert tuple(sum(item_id.startswith(key) for item_id in selected) for key in "ABCD") == (
        6,
        5,
        5,
        4,
    )


def test_stage_includes_unseen_and_uses_weaker_concept_for_multi_tag_item() -> None:
    items = (
        SelectionItem(item_id="mixed", concept_ids=("A", "B")),
        SelectionItem(item_id="b", concept_ids=("B",)),
    )
    selected = select_items(
        "stage_assessment",
        items,
        mastery_by_concept={
            "A": ConceptMastery(0, 0),
            "B": ConceptMastery(10, 9),
        },
        open_wrong_item_ids=(),
        rng=random.Random(0),
    )
    assert selected == ("mixed", "b")


def test_correction_returns_every_current_open_wrong_item() -> None:
    selected = select_items(
        "correction",
        _items(4),
        mastery_by_concept={},
        open_wrong_item_ids=("q04", "missing", "q02"),
        rng=random.Random(0),
    )
    assert selected == ("q02", "q04")


def test_empty_teacher_bank_has_explicit_failure() -> None:
    with pytest.raises(DomainError) as captured:
        select_items(
            "diagnostic",
            (),
            mastery_by_concept={},
            open_wrong_item_ids=(),
            rng=random.Random(0),
        )
    assert captured.value.code == "TEACHER_QUESTION_BANK_EMPTY"


def test_policy_context_replaces_legacy_fixed_blueprint_selection() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    task = make_task_plan().model_copy(update={"task_type": "diagnostic"})
    paper = PaperGenerator().generate(
        task,
        bundle,
        None,
        None,
        AssessmentSelectionContext(
            mastery_by_concept={},
            open_wrong_item_ids=(),
        ),
    )
    assert [item.item_id for item in paper.all_items()] == ["item_2"]
    assert paper.sections[0].name == "诊断测评"
