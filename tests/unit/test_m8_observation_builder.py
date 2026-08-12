"""Authoritative M8-to-M5 observation construction tests."""

from course_insight.modules.m8_assessment_scoring.observation_builder import (
    build_observation_batch,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from tests.factories.m5_m8 import make_paper, make_scoring_bundle


def test_observation_uses_frozen_item_identity_not_instance_name() -> None:
    paper = make_paper(instance_id="random-instance-88", item_id="item_2")
    record = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )

    batch = build_observation_batch(record, make_scoring_bundle(paper))

    assert batch.observations[0].item_id == "item_2"
    assert batch.observations[0].concept_ids == ["concept_2"]
    assert batch.observations[0].response_outcome == "correct"
    assert batch.observations[0].outcome_policy_version == "1.0.0"
