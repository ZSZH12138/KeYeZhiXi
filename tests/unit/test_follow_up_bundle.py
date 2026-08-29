"""Correction reuses the exact lost item without a JSON reconstruction ledger."""

from __future__ import annotations

from course_insight.contracts.knowledge import ParameterRule
from course_insight.modules.m0_platform.follow_up import (
    build_follow_up_bundle,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from tests.factories.m5_m8 import (
    FixedClock,
    UTC_TIME,
    make_knowledge_bundle,
    make_task_plan,
)


def test_follow_up_paper_has_one_exact_source_item() -> None:
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
    assert follow_item.item_id == item.item_id
    assert follow_item.version == item.version
    assert follow_paper.all_items()[0].item_id == item.item_id


def test_follow_up_does_not_replace_the_lost_item_with_a_sibling() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    source = bundle.items[0]
    sibling = source.model_copy(
        update={"item_id": "item_2_variant", "stem": "Variant question"}
    )
    bundle = bundle.model_copy(update={"items": [source, sibling]})
    _, follow_item = build_follow_up_bundle(
        bundle,
        source_item=source,
        salt="paper_1:item_2_instance",
    )

    assert follow_item.item_id == source.item_id
    assert follow_item.stem == source.stem
