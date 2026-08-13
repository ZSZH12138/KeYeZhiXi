"""Canonical, privacy-preserving identity for private intent requests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import MappingProxyType

from course_insight.modules.m4_task_orchestration.normalization import (
    normalize_intent_text,
)


def build_intent_request_identity(
    *,
    course_id: str,
    class_id: str,
    learner_id: str,
    session_id: str,
    knowledge_bundle_id: str,
    course_package_id: str,
    task_type_hint: str | None,
    student_text: str,
) -> Mapping[str, str]:
    """Return an immutable identity without retaining raw learner text."""

    fields = {
        "course_id": course_id,
        "class_id": class_id,
        "learner_id": learner_id,
        "session_id": session_id,
        "knowledge_bundle_id": knowledge_bundle_id,
        "course_package_id": course_package_id,
    }
    for name, value in fields.items():
        _validate_nonblank(name, value)
    return MappingProxyType(
        {
            **fields,
            "task_type_hint": normalize_hint(task_type_hint),
            "input_checksum": input_checksum(student_text),
        }
    )


def input_checksum(student_text: str) -> str:
    """Hash normalized text so raw learner wording is not persisted or logged."""

    if not isinstance(student_text, str):
        raise ValueError("student_text must be a string")
    return hashlib.sha256(
        normalize_intent_text(student_text).encode("utf-8")
    ).hexdigest()


def normalize_hint(task_type_hint: str | None) -> str:
    """Canonicalize an optional hint without deciding whether it is supported."""

    if task_type_hint is None:
        return ""
    if not isinstance(task_type_hint, str):
        raise ValueError("task_type_hint must be a string or None")
    return normalize_intent_text(task_type_hint).replace(" ", "_")
def _validate_nonblank(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
