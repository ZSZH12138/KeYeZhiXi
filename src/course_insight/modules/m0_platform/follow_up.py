"""Build a one-item isomorphic follow-up paper from a lost assessment item."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    QMatrixEntry,
)


def build_follow_up_bundle(
    bundle: KnowledgeBundle,
    *,
    source_item: ItemCard,
    salt: str,
) -> tuple[KnowledgeBundle, ItemCard]:
    """Return a bundle whose approved blueprint contains one follow-up item."""

    del salt
    follow_item = source_item.model_copy(deep=True)
    items = list(bundle.items)
    q_matrix = list(bundle.q_matrix)
    if all(item.item_id != follow_item.item_id for item in items):
        items.append(follow_item)
        q_matrix.extend(_q_entries_for(bundle, source_item, follow_item))
    working = bundle.model_copy(update={"items": items, "q_matrix": q_matrix})
    blueprint = _approved_blueprint(working)
    score = follow_item.max_score(working)
    weights = _concept_weights(follow_item)
    template = blueprint.sections[0] if blueprint.sections else None
    section = BlueprintSection(
        section_id="follow_up",
        name="订正跟练",
        purpose="remediation",
        item_count=1,
        score=score,
        item_types=list(template.item_types) if template is not None else [],
        concept_weights=weights,
        difficulty_range=(
            template.difficulty_range if template is not None else (0, 5)
        ),
        anchor_item_ids=[follow_item.item_id],
        anchor_item_versions={follow_item.item_id: follow_item.version},
    )
    updated = blueprint.model_copy(
        update={"sections": [section], "total_score": score},
        deep=True,
    )
    blueprints = [
        updated if item.blueprint_id == blueprint.blueprint_id else item
        for item in working.blueprints
    ]
    return working.model_copy(update={"blueprints": blueprints}), follow_item


def rebuild_follow_up_bundle(
    bundle: KnowledgeBundle,
    records: Mapping[str, Any],
    follow_up_paper_id: str,
) -> KnowledgeBundle | None:
    """Rebuild the exact bundle frozen when a follow-up paper was started."""

    link = _follow_up_link(records, follow_up_paper_id)
    if link is None:
        return None
    source_paper_id, item_instance_id, source_item_id = link
    prior = merge_follow_up_items(
        bundle,
        records,
        exclude_follow_up_paper_id=follow_up_paper_id,
    )
    try:
        source = prior.get_item(source_item_id)
    except DomainError:
        return None
    follow_bundle, _ = build_follow_up_bundle(
        prior,
        source_item=source,
        salt=f"{source_paper_id}:{item_instance_id}",
    )
    return follow_bundle


def follow_up_source(
    records: Mapping[str, Any],
    follow_up_paper_id: str,
) -> tuple[str, str, str] | None:
    """Return source paper, item instance, and item id for one follow-up paper."""

    return _follow_up_link(records, follow_up_paper_id)


def merge_follow_up_items(
    bundle: KnowledgeBundle,
    records: Mapping[str, Any],
    *,
    exclude_follow_up_paper_id: str | None = None,
) -> KnowledgeBundle:
    """Reattach cloned follow-up items so later scoring can resolve them."""

    items = list(bundle.items)
    q_matrix = list(bundle.q_matrix)
    known = {item.item_id for item in items}
    changed = False
    for payload in records.values():
        stored = payload.get("items", {})
        if type(stored) is not dict:
            continue
        for item in stored.values():
            if (
                exclude_follow_up_paper_id is not None
                and item.get("follow_up_paper_id") == exclude_follow_up_paper_id
            ):
                continue
            clone_id = item.get("follow_up_item_id")
            source_id = item.get("source_item_id")
            if (
                not isinstance(clone_id, str)
                or not isinstance(source_id, str)
                or clone_id in known
            ):
                continue
            try:
                source = bundle.get_item(source_id)
            except DomainError:
                continue
            clone = source.model_copy(update={"item_id": clone_id}, deep=True)
            items.append(clone)
            known.add(clone_id)
            changed = True
            q_matrix.extend(_q_entries_for(bundle, source, clone))
    if not changed:
        return bundle
    return bundle.model_copy(update={"items": items, "q_matrix": q_matrix})


def _follow_up_link(
    records: Mapping[str, Any],
    follow_up_paper_id: str,
) -> tuple[str, str, str] | None:
    for source_paper_id, payload in records.items():
        stored = payload.get("items", {}) if isinstance(payload, dict) else None
        if type(stored) is not dict:
            continue
        for item_instance_id, item in stored.items():
            if (
                type(item) is dict
                and item.get("follow_up_paper_id") == follow_up_paper_id
                and isinstance(item.get("source_item_id"), str)
                and isinstance(item_instance_id, str)
            ):
                return source_paper_id, item_instance_id, str(item["source_item_id"])
    return None


def _approved_blueprint(bundle: KnowledgeBundle):
    for item in bundle.blueprints:
        if " ".join(item.status.split()).casefold() == "teacher_approved":
            return item
    if bundle.blueprints:
        return bundle.blueprints[0]
    raise DomainError(
        code="BLUEPRINT_NOT_FOUND",
        module="m3",
        message="assessment blueprint was not found",
    )


def _q_entries_for(
    bundle: KnowledgeBundle,
    source: ItemCard,
    clone: ItemCard,
) -> list[QMatrixEntry]:
    copied = [
        entry.model_copy(update={"item_id": clone.item_id})
        for entry in bundle.q_matrix
        if entry.item_id == source.item_id and entry.item_version == source.version
    ]
    if copied:
        return copied
    count = len(clone.concept_ids)
    if count == 0:
        return []
    weight = 1.0 if count == 1 else round(1.0 / count, 6)
    return [
        QMatrixEntry(
            item_id=clone.item_id,
            item_version=clone.version,
            concept_id=concept_id,
            weight=weight,
        )
        for concept_id in clone.concept_ids
    ]


def _concept_weights(item: ItemCard) -> dict[str, float]:
    count = len(item.concept_ids)
    if count == 0:
        return {}
    if count == 1:
        return {item.concept_ids[0]: 1.0}
    weight = 1.0 / count
    weights = {concept_id: weight for concept_id in item.concept_ids[:-1]}
    weights[item.concept_ids[-1]] = 1.0 - weight * (count - 1)
    return weights
