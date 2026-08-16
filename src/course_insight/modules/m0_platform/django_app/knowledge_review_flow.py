"""Signed, short-lived bindings for M3 teacher knowledge-package review."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import cast

from django.core import signing

from course_insight.contracts.errors import DomainError


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_SALT = "course-insight.m3.knowledge-review.v1"
_KEYS = frozenset(
    {"v", "actor_id", "course_id", "class_id", "review_id", "review_version", "issued_at"}
)


def issue_knowledge_review_token(
    *,
    actor_id: str,
    course_id: str,
    class_id: str,
    review_id: str,
    review_version: int,
) -> str:
    payload = {
        "v": 1,
        "actor_id": actor_id,
        "course_id": course_id,
        "class_id": class_id,
        "review_id": review_id,
        "review_version": review_version,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }
    _validate(payload)
    return signing.dumps(payload, salt=_SALT, compress=True)


def verify_knowledge_review_token(
    token: str,
    *,
    actor_id: str,
    course_id: str,
    class_id: str,
    review_id: str,
    review_version: int,
    max_age_seconds: int,
) -> dict[str, object]:
    if not isinstance(token, str) or not token or len(token) > 2048:
        _invalid()
    try:
        raw = signing.loads(token, salt=_SALT, max_age=max_age_seconds)
        payload = _validate(raw)
    except (signing.BadSignature, TypeError, ValueError):
        _invalid()
    expected = {
        "v": 1,
        "actor_id": actor_id,
        "course_id": course_id,
        "class_id": class_id,
        "review_id": review_id,
        "review_version": review_version,
        "issued_at": payload["issued_at"],
    }
    if payload != expected:
        _invalid()
    return payload


def _validate(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _KEYS:
        _invalid()
    payload = cast(dict[str, object], value)
    if (
        payload["v"] != 1
        or any(not _IDENTIFIER.fullmatch(payload[key]) for key in ("actor_id", "course_id", "class_id", "review_id"))
        or type(payload["review_version"]) is not int
        or payload["review_version"] < 1
        or not isinstance(payload["issued_at"], str)
        or not payload["issued_at"].endswith("Z")
    ):
        _invalid()
    return dict(payload)


def _invalid() -> None:
    raise DomainError(
        code="WEB_FLOW_TOKEN_INVALID",
        module="m0",
        message="the knowledge review workflow token is invalid or expired",
        recoverable=True,
    )

