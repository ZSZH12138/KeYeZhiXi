"""Pure validation and compatibility helpers for stored M4 intent decisions."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from course_insight.modules.m4_task_orchestration.intent import (
    IntentStatus,
    _normalize_reason_codes,
    _validate_safe_token,
)
from course_insight.modules.m4_task_orchestration.routing import (
    SUPPORTED_TASK_TYPES,
)


def validated_reason_codes(value: object) -> tuple[str, ...]:
    """Return bounded safe audit codes that cannot carry free-form text."""

    return _normalize_reason_codes(value)


def validate_status_and_label(
    status: object,
    label: object,
    *,
    prefix: str,
) -> None:
    if not isinstance(status, IntentStatus):
        raise ValueError(f"{prefix} status is invalid")
    if label is not None and label not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"{prefix} task type is unsupported")
    if status is IntentStatus.ACCEPTED and label is None:
        raise ValueError(f"accepted {prefix} requires a task type")
    if status is not IntentStatus.ACCEPTED and label is not None:
        raise ValueError(f"non-accepted {prefix} cannot have a task type")


def validate_optional_probability(name: str, value: object) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{name} must be a finite probability or None")


def normalized_probability(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except OverflowError:
        return math.inf


def legacy_v1_integer_score_payload_checksum(
    canonical_payload: dict[str, object],
) -> str:
    payload = {
        **canonical_payload,
        "confidence": _legacy_v1_integer_score(canonical_payload["confidence"]),
        "margin": _legacy_v1_integer_score(canonical_payload["margin"]),
    }
    shadow = canonical_payload["shadow"]
    if type(shadow) is dict:
        payload["shadow"] = {
            **shadow,
            "confidence": _legacy_v1_integer_score(shadow["confidence"]),
            "margin": _legacy_v1_integer_score(shadow["margin"]),
        }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_nonblank(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def validate_audit_token(name: str, value: object) -> None:
    _validate_safe_token(name, value, max_length=128)


def validate_sha256(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def validate_created_at(value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("created_at must be timezone-aware")


def _legacy_v1_integer_score(value: object) -> object:
    if (
        type(value) is float
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
        and value.is_integer()
    ):
        return int(value)
    return value
