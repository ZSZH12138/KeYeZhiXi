"""Teacher-authored extra objective answers layered on an approved bundle."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from course_insight.contracts.knowledge import KnowledgeBundle


def merge_objective_answers(
    bundle: KnowledgeBundle,
    overlays: Mapping[str, list[str]],
) -> KnowledgeBundle:
    """Return a copy whose objective items include teacher-listed strings."""

    if not overlays:
        return bundle
    items = []
    for item in bundle.items:
        extra = overlays.get(item.item_id)
        if extra is None or not item.is_objective():
            items.append(item.model_copy(deep=True))
            continue
        cleaned = [
            value.strip()
            for value in extra
            if isinstance(value, str) and value.strip()
        ]
        if not cleaned:
            items.append(item.model_copy(deep=True))
            continue
        key = dict(item.answer_key)
        key["answers"] = cleaned
        key.setdefault("answer", cleaned[0])
        items.append(item.model_copy(update={"answer_key": key}, deep=True))
    return bundle.model_copy(update={"items": items})


def overlay_path(state_policy_path: Path) -> Path:
    return Path(state_policy_path).resolve().parent / "objective_answer_overlays.json"


def load_overlays(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if type(payload) is not dict:
        return {}
    overlays: dict[str, list[str]] = {}
    for item_id, answers in payload.items():
        if not isinstance(item_id, str) or type(answers) is not list:
            continue
        cleaned = [
            value.strip()
            for value in answers
            if isinstance(value, str) and value.strip()
        ]
        if cleaned:
            overlays[item_id] = cleaned
    return overlays


def save_overlays(path: Path, overlays: Mapping[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(overlays), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def bundle_with_overlays(
    bundle: KnowledgeBundle,
    state_policy_path: Path,
) -> KnowledgeBundle:
    return merge_objective_answers(
        bundle,
        load_overlays(overlay_path(state_policy_path)),
    )
