"""Teacher-authored diagnostic blueprint sections layered on an approved bundle."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    KnowledgeBundle,
)

_FOUR_PURPOSES = ("anchor", "uncertainty", "misconception", "remediation")
_FOUR_NAMES = ("公共锚点", "不确定点", "误区", "补救")


def merge_blueprint_overlay(
    bundle: KnowledgeBundle,
    overlay: Mapping[str, Any] | None,
) -> KnowledgeBundle:
    """Return a copy whose approved blueprint sections follow the teacher overlay."""

    if not overlay:
        return bundle
    specs = overlay.get("sections")
    if type(specs) is not list or not specs:
        return bundle
    target = _target_blueprint(bundle, overlay.get("blueprint_id"))
    if target is None:
        return bundle
    template = target.sections[0] if target.sections else None
    sections: list[BlueprintSection] = []
    for spec in specs:
        if type(spec) is not dict:
            continue
        sections.append(_section_from_spec(spec, template, bundle))
    if not sections:
        return bundle
    total = sum(section.score for section in sections)
    updated = target.model_copy(
        update={"sections": sections, "total_score": total},
        deep=True,
    )
    blueprints = [
        updated if item.blueprint_id == target.blueprint_id else item
        for item in bundle.blueprints
    ]
    return bundle.model_copy(update={"blueprints": blueprints})


def overlay_path(state_policy_path: Path) -> Path:
    return Path(state_policy_path).resolve().parent / "blueprint_overlays.json"


def load_overlay(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if type(payload) is dict else {}


def save_overlay(path: Path, overlay: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(overlay), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def default_overlay(bundle: KnowledgeBundle) -> dict[str, Any]:
    target = _target_blueprint(bundle, None)
    if target is None:
        return {"sections": []}
    existing = list(target.sections)
    sections: list[dict[str, Any]] = []
    for index, purpose in enumerate(_FOUR_PURPOSES):
        source = existing[index] if index < len(existing) else (
            existing[0] if existing else None
        )
        if source is None:
            sections.append(
                {
                    "section_id": f"sec_{purpose}",
                    "name": _FOUR_NAMES[index],
                    "purpose": purpose,
                    "item_count": 1,
                    "score": 1.0,
                    "anchor_item_ids": [],
                }
            )
            continue
        sections.append(
            {
                "section_id": source.section_id if index < len(existing) else (
                    f"sec_{purpose}"
                ),
                "name": source.name if index < len(existing) else _FOUR_NAMES[index],
                "purpose": source.purpose or purpose,
                "item_count": source.item_count,
                "score": source.score,
                "anchor_item_ids": list(source.anchor_item_ids),
            }
        )
    return {"blueprint_id": target.blueprint_id, "sections": sections}


def bundle_with_blueprint(
    bundle: KnowledgeBundle,
    state_policy_path: Path,
) -> KnowledgeBundle:
    return merge_blueprint_overlay(bundle, load_overlay(overlay_path(state_policy_path)))


def _target_blueprint(
    bundle: KnowledgeBundle,
    blueprint_id: object,
) -> AssessmentBlueprint | None:
    if isinstance(blueprint_id, str) and blueprint_id:
        for item in bundle.blueprints:
            if item.blueprint_id == blueprint_id:
                return item
        return None
    for item in bundle.blueprints:
        if " ".join(item.status.split()).casefold() == "teacher_approved":
            return item
    return bundle.blueprints[0] if bundle.blueprints else None


def _section_from_spec(
    spec: dict[str, Any],
    template: BlueprintSection | None,
    bundle: KnowledgeBundle,
) -> BlueprintSection:
    anchors = [
        value.strip()
        for value in spec.get("anchor_item_ids") or []
        if isinstance(value, str) and value.strip()
    ]
    found_anchors: list[str] = []
    versions: dict[str, str] = {}
    for item_id in anchors:
        try:
            item = bundle.get_item(item_id)
        except DomainError:
            continue
        found_anchors.append(item_id)
        versions[item_id] = item.version
    weights = dict(template.concept_weights) if template is not None else {}
    if not weights and bundle.concepts:
        concept_id = bundle.concepts[0].concept_id
        weights = {concept_id: 1.0}
    return BlueprintSection(
        section_id=str(spec.get("section_id") or "section"),
        name=str(spec.get("name") or "Section"),
        purpose=(
            str(spec["purpose"])
            if spec.get("purpose") not in {None, ""}
            else None
        ),
        item_count=int(spec.get("item_count") or 0),
        score=float(spec.get("score") or 0.0),
        item_types=list(template.item_types) if template is not None else [],
        concept_weights=weights,
        difficulty_range=(
            template.difficulty_range if template is not None else (0, 5)
        ),
        anchor_item_ids=found_anchors,
        anchor_item_versions=versions,
    )
