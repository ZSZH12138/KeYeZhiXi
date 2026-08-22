"""Correction guide exposes error cause, hint, follow-up, and per-item notes."""

from __future__ import annotations

from course_insight.modules.m0_platform.correction_records import (
    record_follow_up_outcome,
    record_hint,
    upsert_lost_items,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    correction_guide_view,
    item_correction_notes,
    student_profile_view,
)
from tests.factories.m5_m8 import (
    UTC_TIME,
    make_knowledge_bundle,
    make_paper,
    make_scoring_bundle,
)
from course_insight.contracts.state import LearnerStateSnapshot


def test_correction_guide_lists_lost_items_without_revealing_the_answer_key() -> None:
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper, score=0.0)
    guide = correction_guide_view(
        paper,
        scoring,
        make_knowledge_bundle(subjective=False),
        hint_revealed=False,
    )

    assert guide.available is True
    assert guide.lost_items
    assert "标准答案" not in guide.hint
    assert guide.hint_revealed is False


def test_item_correction_notes_are_per_lost_item() -> None:
    paper = make_paper(subjective=False, concept_ids=["concept_2"])
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
                "cause": "lost",
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

    assert notes[0].status == "提示后改对"
    assert notes[0].item_instance_id == instance_id


def test_profile_uses_concept_names_and_progress_points() -> None:
    bundle = make_knowledge_bundle()
    snapshot = LearnerStateSnapshot(
        snapshot_id="snapshot_1",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        state_version=2,
        concept_states=[],
        overall_mastery=0.6,
        evidence_count=2,
        updated_at=UTC_TIME,
    )
    profile = student_profile_view(
        snapshot=snapshot,
        knowledge_bundle=bundle,
        mastered_threshold=0.8,
        consolidating_threshold=0.4,
        misconception_activation_threshold=0.5,
        progress=((1, 0.3), (2, 0.6)),
    )

    assert profile.progress == ((1, 0.3), (2, 0.6))
    assert profile.next_practice_names == ()
