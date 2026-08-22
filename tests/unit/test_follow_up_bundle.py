"""Follow-up practice reuses the lost concept with a different frozen instance."""

from __future__ import annotations

from course_insight.contracts.knowledge import ParameterRule
from course_insight.modules.m0_platform.follow_up import build_follow_up_bundle
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from tests.factories.m5_m8 import (
    FixedClock,
    UTC_TIME,
    make_knowledge_bundle,
    make_task_plan,
)


def test_follow_up_paper_has_one_item_and_different_parameters() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    item = bundle.items[0].model_copy(
        update={
            "stem": "Write the selected protocol {protocol}.",
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
            "answer_key": {
                "answer": "{protocol}",
                "answers": ["{protocol}"],
                "max_score": 1.0,
            },
        }
    )
    bundle = bundle.model_copy(update={"items": [item]})
    original = PaperGenerator(FixedClock(UTC_TIME)).generate(
        make_task_plan(),
        bundle,
        None,
        None,
    )
    follow_bundle, follow_item = build_follow_up_bundle(
        bundle,
        source_item=item,
        salt="paper_1:random-instance-88",
    )
    follow_paper = PaperGenerator(FixedClock(UTC_TIME)).generate(
        make_task_plan(),
        follow_bundle,
        None,
        None,
    )

    assert len(follow_paper.all_items()) == 1
    assert set(follow_paper.all_items()[0].concept_ids) & set(item.concept_ids)
    assert follow_item.item_id != item.item_id or (
        follow_paper.all_items()[0].parameters.get("protocol")
        != original.all_items()[0].parameters.get("protocol")
    )
