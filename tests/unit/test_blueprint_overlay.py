"""Teacher blueprint overlay can replace sections without mutating the CAS bundle."""

from __future__ import annotations

from course_insight.modules.m0_platform.blueprint_overlay import (
    merge_blueprint_overlay,
)
from tests.factories.m5_m8 import make_knowledge_bundle


def test_teacher_can_define_four_purpose_sections_on_a_copy() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    original = bundle.blueprints[0]
    merged = merge_blueprint_overlay(
        bundle,
        {
            "blueprint_id": original.blueprint_id,
            "sections": [
                {
                    "section_id": "sec_anchor",
                    "name": "公共锚点",
                    "purpose": "anchor",
                    "item_count": 1,
                    "score": 1.0,
                    "anchor_item_ids": [bundle.items[0].item_id],
                },
                {
                    "section_id": "sec_uncertainty",
                    "name": "不确定点",
                    "purpose": "uncertainty",
                    "item_count": 1,
                    "score": 1.0,
                    "anchor_item_ids": [],
                },
                {
                    "section_id": "sec_misconception",
                    "name": "误区",
                    "purpose": "misconception",
                    "item_count": 1,
                    "score": 1.0,
                    "anchor_item_ids": [],
                },
                {
                    "section_id": "sec_remediation",
                    "name": "补救",
                    "purpose": "remediation",
                    "item_count": 1,
                    "score": 1.0,
                    "anchor_item_ids": [],
                },
            ],
        },
    )
    updated = merged.get_blueprint(original.blueprint_id)

    assert [section.purpose for section in updated.sections] == [
        "anchor",
        "uncertainty",
        "misconception",
        "remediation",
    ]
    assert updated.total_score == 4.0
    assert bundle.blueprints[0].sections[0].purpose is None
    assert len(bundle.blueprints[0].sections) == 1
