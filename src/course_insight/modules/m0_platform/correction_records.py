"""Per-item correction records stored beside course runtime policy."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def records_path(state_policy_path: Path) -> Path:
    return Path(state_policy_path).resolve().parent / "correction_records.json"


def load_records(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if type(payload) is dict else {}


def save_records(path: Path, records: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(records), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def upsert_lost_items(
    records: Mapping[str, Any],
    *,
    paper_id: str,
    learner_id: str,
    course_id: str,
    class_id: str,
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    updated = _copy_records(records)
    current = updated.setdefault(
        paper_id,
        {
            "learner_id": learner_id,
            "course_id": course_id,
            "class_id": class_id,
            "items": {},
        },
    )
    stored = current.setdefault("items", {})
    for item in items:
        instance_id = str(item["item_instance_id"])
        previous = stored.get(instance_id, {})
        stored[instance_id] = {
            **previous,
            "stem": str(item.get("stem") or previous.get("stem") or ""),
            "cause": str(item.get("cause") or previous.get("cause") or ""),
            "concept_ids": list(
                item.get("concept_ids") or previous.get("concept_ids") or []
            ),
            "hint_revealed": bool(previous.get("hint_revealed")),
            "follow_up_paper_id": previous.get("follow_up_paper_id"),
            "follow_up_item_id": previous.get("follow_up_item_id"),
            "source_item_id": previous.get("source_item_id"),
            "follow_up_correct": previous.get("follow_up_correct"),
        }
    return updated


def record_hint(
    records: Mapping[str, Any],
    paper_id: str,
    item_instance_id: str,
) -> dict[str, Any]:
    updated = _copy_records(records)
    item = (
        updated.get(paper_id, {})
        .get("items", {})
        .get(item_instance_id)
    )
    if type(item) is dict:
        item["hint_revealed"] = True
    return updated


def link_follow_up(
    records: Mapping[str, Any],
    *,
    paper_id: str,
    item_instance_id: str,
    follow_up_paper_id: str,
    follow_up_item_id: str,
    source_item_id: str,
) -> dict[str, Any]:
    updated = _copy_records(records)
    item = (
        updated.get(paper_id, {})
        .get("items", {})
        .get(item_instance_id)
    )
    if type(item) is dict:
        item["follow_up_paper_id"] = follow_up_paper_id
        item["follow_up_item_id"] = follow_up_item_id
        item["source_item_id"] = source_item_id
    return updated


def record_follow_up_outcome(
    records: Mapping[str, Any],
    *,
    follow_up_paper_id: str,
    follow_up_correct: bool,
    source_paper_id: str | None = None,
    source_item_instance_id: str | None = None,
) -> dict[str, Any]:
    updated = _copy_records(records)
    if source_paper_id and source_item_instance_id:
        item = (
            updated.get(source_paper_id, {})
            .get("items", {})
            .get(source_item_instance_id)
        )
        if type(item) is dict:
            item["follow_up_paper_id"] = follow_up_paper_id
            item["follow_up_correct"] = bool(follow_up_correct)
        return updated
    for payload in updated.values():
        items = payload.get("items", {})
        if type(items) is not dict:
            continue
        for item in items.values():
            if type(item) is dict and item.get("follow_up_paper_id") == follow_up_paper_id:
                item["follow_up_correct"] = bool(follow_up_correct)
                return updated
    for payload in updated.values():
        items = payload.get("items", {})
        if type(items) is not dict or len(items) != 1:
            continue
        item = next(iter(items.values()))
        if type(item) is dict:
            item["follow_up_paper_id"] = follow_up_paper_id
            item["follow_up_correct"] = bool(follow_up_correct)
            return updated
    return updated


def correction_status(item: Mapping[str, Any]) -> str:
    if item.get("follow_up_correct") is True:
        if item.get("hint_revealed"):
            return "提示后改对"
        return "独立改对"
    return "尚未订正"


def class_correction_rates(
    records: Mapping[str, Any],
    *,
    course_id: str,
    class_id: str,
) -> tuple[float | None, float | None]:
    items: list[Mapping[str, Any]] = []
    for payload in records.values():
        if (
            payload.get("course_id") != course_id
            or payload.get("class_id") != class_id
        ):
            continue
        stored = payload.get("items", {})
        if type(stored) is dict:
            items.extend(
                item for item in stored.values() if type(item) is dict
            )
    if not items:
        return (None, None)
    completion = sum(
        1 for item in items if correction_status(item) != "尚未订正"
    ) / len(items)
    hinted = [item for item in items if item.get("hint_revealed")]
    post_hint = None
    if hinted:
        post_hint = sum(
            1 for item in hinted if correction_status(item) == "提示后改对"
        ) / len(hinted)
    return (completion, post_hint)


def _copy_records(records: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for paper_id, payload in records.items():
        if type(payload) is not dict:
            continue
        items = payload.get("items", {})
        copied_items = {
            item_id: dict(item)
            for item_id, item in items.items()
            if type(item) is dict
        }
        copied[str(paper_id)] = {**payload, "items": copied_items}
    return copied
