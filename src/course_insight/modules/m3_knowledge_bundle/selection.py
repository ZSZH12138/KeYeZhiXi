"""Pure, version-aware blueprint selection shared by M3 and M8."""

from __future__ import annotations

import math

from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
)


_SCORE_TOLERANCE = 1e-9


class BlueprintSelectionError(ValueError):
    """The approved bundle cannot satisfy one blueprint deterministically."""


def select_blueprint_items(
    bundle: KnowledgeBundle,
    blueprint: AssessmentBlueprint,
) -> tuple[tuple[ItemCard, ...], ...]:
    """Select exact anchors, objective items, then subjective items by source order.

    Item identifiers are globally single-use across blueprint sections.  New
    blueprints bind every anchor to an exact version; a legacy empty version map
    is accepted only when the anchor has one eligible approved version.
    """

    try:
        validated_bundle = KnowledgeBundle.model_validate(bundle)
        validated_blueprint = AssessmentBlueprint.model_validate(blueprint)
    except Exception:
        raise BlueprintSelectionError("blueprint input is invalid") from None

    matching_blueprints = [
        candidate
        for candidate in validated_bundle.blueprints
        if candidate.blueprint_id == validated_blueprint.blueprint_id
        and candidate.version == validated_blueprint.version
    ]
    if (
        validated_blueprint.course_id != validated_bundle.course_id
        or len(matching_blueprints) != 1
        or matching_blueprints[0].to_dict() != validated_blueprint.to_dict()
    ):
        raise BlueprintSelectionError("blueprint is not bound to the bundle")

    used_item_ids: set[str] = set()
    selected_sections: list[tuple[ItemCard, ...]] = []
    for section in validated_blueprint.sections:
        selected = _select_section(
            bundle=validated_bundle,
            section=section,
            used_item_ids=used_item_ids,
        )
        selected_sections.append(tuple(item.model_copy(deep=True) for item in selected))
        used_item_ids.update(item.item_id for item in selected)
    return tuple(selected_sections)


def _select_section(
    *,
    bundle: KnowledgeBundle,
    section: BlueprintSection,
    used_item_ids: set[str],
) -> list[ItemCard]:
    eligible = [
        item
        for item in bundle.items
        if item.is_approved()
        and item.item_id not in used_item_ids
        and section.accepts(item)
    ]

    anchors: list[ItemCard] = []
    anchored_ids: set[str] = set()
    for anchor_id in section.anchor_item_ids:
        if anchor_id in anchored_ids:
            raise BlueprintSelectionError("anchor item is duplicated")
        candidates = [item for item in eligible if item.item_id == anchor_id]
        if section.anchor_item_versions:
            anchor_version = section.anchor_item_versions.get(anchor_id)
            candidates = [
                item for item in candidates if item.version == anchor_version
            ]
        if len(candidates) != 1:
            raise BlueprintSelectionError("anchor item version is unavailable")
        anchors.append(candidates[0])
        anchored_ids.add(anchor_id)

    if len(anchors) > section.item_count:
        raise BlueprintSelectionError("anchor count exceeds item count")

    remaining = [item for item in eligible if item.item_id not in anchored_ids]
    ordered = [
        *[item for item in remaining if item.is_objective()],
        *[item for item in remaining if not item.is_objective()],
    ]
    selected = list(anchors)
    selected_ids = set(anchored_ids)
    for item in ordered:
        if len(selected) == section.item_count:
            break
        if item.item_id in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item.item_id)

    if len(selected) != section.item_count:
        raise BlueprintSelectionError("not enough unique approved items")
    try:
        selected_score = math.fsum(item.max_score(bundle) for item in selected)
    except Exception:
        raise BlueprintSelectionError("selected item score is invalid") from None
    if not math.isclose(
        selected_score,
        section.score,
        rel_tol=0.0,
        abs_tol=_SCORE_TOLERANCE,
    ):
        raise BlueprintSelectionError("selected item score does not match section")
    return selected


__all__ = ["BlueprintSelectionError", "select_blueprint_items"]
