"""Authoritative M8-to-M5 observation construction tests."""

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m8_assessment_scoring.observation_builder import (
    build_observation_batch,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
    ModelObservationPolicy,
)
from tests.factories.m5_m8 import (
    make_paper,
    make_rubric,
    make_scoring_bundle,
)


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


def test_frozen_record_rejects_modified_paper_and_rubric_gaps() -> None:
    tampered = make_paper().model_copy(
        update={"immutable_checksum": "tampered"}
    )
    with pytest.raises(DomainError) as paper_error:
        FrozenAssessmentRecord(
            paper=tampered,
            course_id="course_1",
            class_id="class_1",
            frozen_rubrics=[],
        )
    assert paper_error.value.code == "PAPER_FREEZE_INVALID"

    with pytest.raises(DomainError) as rubric_error:
        FrozenAssessmentRecord(
            paper=make_paper(subjective=True),
            course_id="course_1",
            class_id="class_1",
            frozen_rubrics=[],
        )
    assert rubric_error.value.code == "FROZEN_RUBRIC_REFERENCE_MISMATCH"


def test_frozen_record_returns_exact_rubric_or_explicit_error() -> None:
    record = FrozenAssessmentRecord(
        paper=make_paper(subjective=True),
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric()],
    )

    assert record.get_rubric("rubric_1") == make_rubric()
    with pytest.raises(DomainError) as missing:
        record.get_rubric("rubric_missing")
    assert missing.value.code == "FROZEN_RUBRIC_NOT_FOUND"


@pytest.mark.parametrize(
    ("version", "threshold", "item_types", "message"),
    [
        (" ", 1.0, frozenset({"objective"}), "version"),
        ("1.0.0", 1.1, frozenset({"objective"}), "threshold"),
        ("1.0.0", 1.0, frozenset(), "at least one"),
    ],
)
def test_observation_policy_rejects_invalid_configuration(
    version: str,
    threshold: float,
    item_types: frozenset[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ModelObservationPolicy(version, threshold, item_types)


def test_observation_policy_rejects_excluded_types_and_invalid_scores() -> None:
    policy = ModelObservationPolicy("1.0.0", 0.5, frozenset({"objective"}))

    with pytest.raises(DomainError) as excluded:
        policy.classify(score=1.0, max_score=1.0, item_type="subjective")
    assert excluded.value.code == "OBSERVATION_ITEM_TYPE_NOT_ALLOWED"

    with pytest.raises(DomainError) as invalid_score:
        policy.classify(score=2.0, max_score=1.0, item_type="objective")
    assert invalid_score.value.code == "LEARNING_OBSERVATION_INVALID"
    assert policy.classify(
        score=0.5,
        max_score=1.0,
        item_type="objective",
    ) == "correct"
    assert policy.classify(
        score=0.0,
        max_score=1.0,
        item_type="objective",
    ) == "incorrect"
