"""Human-readable labels for opaque course and class scope identifiers.

The opaque identifiers remain the only values used in URLs, permissions, and
database relationships.  This module is deliberately presentation-only, so a
teacher's chosen names can be shown everywhere without weakening exact-scope
authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_LEGACY_COURSE_NAMES = {
    "course_network": "计算机网络核心原理与故障诊断",
}
_LEGACY_CLASS_NUMBER = re.compile(r"^class_(\d+)$")


def course_label(course_id: str, display_name: object) -> str:
    """Return the teacher-facing course name without exposing a blank label."""

    normalized = _normalized(display_name)
    if normalized:
        return normalized
    return _LEGACY_COURSE_NAMES.get(course_id, "未命名课程")


def class_label(class_id: str, display_name: object) -> str:
    """Return the teacher-facing class name without exposing a blank label."""

    normalized = _normalized(display_name)
    if normalized:
        return normalized
    matched = _LEGACY_CLASS_NUMBER.fullmatch(class_id)
    if matched is not None:
        return f"{matched.group(1)}班"
    return "未命名班级"


@dataclass(frozen=True, slots=True)
class ScopeDisplay:
    """One exact backend scope paired with its display-only names."""

    course_id: str
    class_id: str
    course_name: str
    class_name: str

    @property
    def label(self) -> str:
        return f"{self.course_name} / {self.class_name}"


def scope_display(
    *,
    course_id: str,
    class_id: str,
    course_display_name: object = "",
    class_display_name: object = "",
) -> ScopeDisplay:
    """Build a display model while retaining the exact scope IDs separately."""

    return ScopeDisplay(
        course_id=course_id,
        class_id=class_id,
        course_name=course_label(course_id, course_display_name),
        class_name=class_label(class_id, class_display_name),
    )


def _normalized(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""
