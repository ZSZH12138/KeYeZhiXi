"""Teacher-listed extra objective answers merge into the approved item key."""

from __future__ import annotations

from course_insight.modules.m0_platform.objective_answers import (
    merge_objective_answers,
)
from tests.factories.m5_m8 import make_knowledge_bundle


def test_teacher_can_add_extra_accepted_strings_without_dropping_max_score() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    item = bundle.items[0]
    merged = merge_objective_answers(
        bundle,
        {item.item_id: ["IPv6", "ipv6"]},
    )
    updated = merged.get_item(item.item_id, item.version)

    assert updated.answer_key["answers"] == ["IPv6", "ipv6"]
    assert updated.answer_key["max_score"] == item.answer_key["max_score"]
    assert bundle.items[0].answer_key != updated.answer_key
