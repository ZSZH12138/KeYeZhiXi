"""Follow-up practice reuses the lost concept with a different frozen instance."""

from __future__ import annotations

from course_insight.contracts.knowledge import ParameterRule
from course_insight.modules.m0_platform.follow_up import (
    build_follow_up_bundle,
    rebuild_follow_up_bundle,
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


def test_rebuild_follow_up_bundle_matches_start_checksum() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    item = bundle.items[0]
    salt_paper = "paper_lost_1"
    instance_id = "item_2_instance"
    follow_bundle, follow_item = build_follow_up_bundle(
        bundle,
        source_item=item,
        salt=f"{salt_paper}:{instance_id}",
    )
    records = {
        salt_paper: {
            "items": {
                instance_id: {
                    "follow_up_paper_id": "paper_fu_1",
                    "follow_up_item_id": follow_item.item_id,
                    "source_item_id": item.item_id,
                }
            }
        }
    }

    rebuilt = rebuild_follow_up_bundle(bundle, records, "paper_fu_1")

    assert rebuilt is not None
    assert rebuilt.content_checksum() == follow_bundle.content_checksum()
    assert bundle.content_checksum() != follow_bundle.content_checksum()


def test_follow_up_prefers_a_same_concept_sibling() -> None:
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

    assert follow_item.item_id == "item_2_variant"
    assert follow_item.stem == "Variant question"
