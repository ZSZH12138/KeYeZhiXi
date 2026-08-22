"""Per-item correction records, not concept-level aggregates, drive status."""

from __future__ import annotations

from course_insight.modules.m0_platform.correction_records import (
    correction_status,
    record_follow_up_outcome,
    record_hint,
    upsert_lost_items,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    item_correction_notes,
)
from tests.factories.m5_m8 import make_paper, make_scoring_bundle


def test_hint_then_correct_follow_up_is_hint_assisted() -> None:
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper, score=0.0)
    instance_id = paper.all_items()[0].item_instance_id
    records = upsert_lost_items(
        {},
        paper_id=paper.paper_id,
        learner_id=paper.learner_id,
        course_id="course_1",
        class_id="class_1",
        items=(
            {
                "item_instance_id": instance_id,
                "stem": "Question",
                "cause": "The normalized answer does not match.",
                "concept_ids": ["concept_2"],
            },
        ),
    )
    records = record_hint(records, paper.paper_id, instance_id)
    records = record_follow_up_outcome(
        records,
        follow_up_paper_id="paper_fu",
        follow_up_correct=True,
    )
    notes = item_correction_notes(paper, scoring, records)

    assert correction_status(records[paper.paper_id]["items"][instance_id]) == (
        "提示后改对"
    )
    assert notes[0].status == "提示后改对"


def test_independent_follow_up_without_hint() -> None:
    paper = make_paper(subjective=False)
    instance_id = paper.all_items()[0].item_instance_id
    records = upsert_lost_items(
        {},
        paper_id=paper.paper_id,
        learner_id=paper.learner_id,
        course_id="course_1",
        class_id="class_1",
        items=(
            {
                "item_instance_id": instance_id,
                "stem": "Question",
                "cause": "lost",
                "concept_ids": ["concept_2"],
            },
        ),
    )
    records = record_follow_up_outcome(
        records,
        follow_up_paper_id="paper_fu",
        source_paper_id=paper.paper_id,
        source_item_instance_id=instance_id,
        follow_up_correct=True,
    )

    assert correction_status(records[paper.paper_id]["items"][instance_id]) == (
        "独立改对"
    )


def test_missing_follow_up_stays_uncorrected_even_if_concept_looks_done() -> None:
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper, score=0.0)
    notes = item_correction_notes(paper, scoring, {})

    assert notes[0].status == "尚未订正"
