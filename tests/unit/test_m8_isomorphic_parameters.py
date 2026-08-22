"""Isomorphic item parameters stay learner-stable and within approved choices."""

from __future__ import annotations

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import BlueprintSection, ParameterRule
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from tests.factories.m5_m8 import (
    FixedClock,
    UTC_TIME,
    make_knowledge_bundle,
    make_task_plan,
)


def _choice_bundle():
    bundle = make_knowledge_bundle(subjective=False)
    item = bundle.items[0].model_copy(
        update={
            "stem": "Which protocol uses {protocol}?",
            "parameter_rules": [
                ParameterRule(
                    name="protocol",
                    value_type="string",
                    minimum=None,
                    maximum=None,
                    choices=["IPv4", "IPv6", "TCP", "UDP"],
                    constraints=[],
                )
            ],
        }
    )
    return bundle.model_copy(update={"items": [item]})


def test_same_learner_reopens_the_same_parameter_choice() -> None:
    bundle = _choice_bundle()
    generator = PaperGenerator(FixedClock(UTC_TIME))

    first = generator.generate(make_task_plan(), bundle, None, None)
    second = generator.generate(make_task_plan(), bundle, None, None)

    assert first.all_items()[0].parameters == second.all_items()[0].parameters
    assert first.all_items()[0].stem == second.all_items()[0].stem
    assert first.all_items()[0].parameters["protocol"] in {
        "IPv4",
        "IPv6",
        "TCP",
        "UDP",
    }
    assert "{" not in first.all_items()[0].stem


def test_different_learners_can_receive_different_approved_choices() -> None:
    bundle = _choice_bundle()
    generator = PaperGenerator(FixedClock(UTC_TIME))
    seen = {
        generator.generate(
            make_task_plan(learner_id=f"learner_{index}"),
            bundle,
            None,
            None,
        ).all_items()[0].parameters["protocol"]
        for index in range(16)
    }

    assert len(seen) > 1
    assert seen <= {"IPv4", "IPv6", "TCP", "UDP"}


def test_invalid_blueprint_section_purpose_is_rejected() -> None:
    with pytest.raises(DomainError) as error:
        BlueprintSection(
            section_id="section_1",
            name="Section",
            item_count=1,
            score=1.0,
            purpose="adaptive",
            item_types=[],
            concept_weights={"concept_2": 1.0},
            difficulty_range=(0, 5),
            anchor_item_ids=[],
        )

    assert error.value.code == "BLUEPRINT_SECTION_PURPOSE_INVALID"
