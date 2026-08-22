"""Short-lived signed bindings for multi-request Web workflows."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, cast

from django.core import signing

from course_insight.contracts.errors import DomainError


FlowPurpose = Literal[
    "assessment_start",
    "assessment_submit",
    "student_result",
    "student_feedback",
    "teacher_review",
    "teacher_rescore",
    "teacher_suggestion",
]
_PURPOSES = frozenset(
    {
        "assessment_start",
        "assessment_submit",
        "student_result",
        "student_feedback",
        "teacher_review",
        "teacher_rescore",
        "teacher_suggestion",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_TOKEN_SALT = "course-insight.m0.web-flow.v1"
_TOKEN_KEYS = frozenset(
    {
        "v",
        "purpose",
        "actor_id",
        "course_id",
        "class_id",
        "paper_id",
        "audit_id",
        "audit_version",
        "issued_at",
    }
)


def issue_flow_token(
    *,
    purpose: FlowPurpose,
    actor_id: str,
    course_id: str,
    class_id: str,
    paper_id: str | None = None,
    audit_id: str | None = None,
    audit_version: int | None = None,
) -> str:
    """Sign scope, object/version identity, and a signer timestamp."""

    payload = _validated_payload(
        {
            "v": 1,
            "purpose": purpose,
            "actor_id": actor_id,
            "course_id": course_id,
            "class_id": class_id,
            "paper_id": paper_id,
            "audit_id": audit_id,
            "audit_version": audit_version,
            "issued_at": _format_utc(datetime.now(timezone.utc)),
        }
    )
    return signing.dumps(payload, salt=_TOKEN_SALT, compress=True)


def verify_flow_token(
    token: str,
    *,
    purpose: FlowPurpose,
    actor_id: str,
    course_id: str,
    class_id: str,
    max_age_seconds: int,
    paper_id: str | None = None,
    audit_id: str | None = None,
    audit_version: int | None = None,
) -> dict[str, object]:
    """Verify expiry and every server-owned identity without disclosure."""

    if (
        not isinstance(token, str)
        or not token
        or len(token) > 2_048
        or isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds < 1
    ):
        _invalid_token()
    try:
        raw = signing.loads(
            token,
            salt=_TOKEN_SALT,
            max_age=max_age_seconds,
        )
        payload = _validated_payload(raw)
    except (signing.BadSignature, TypeError, ValueError):
        _invalid_token()
    expected = {
        "v": 1,
        "purpose": purpose,
        "actor_id": actor_id,
        "course_id": course_id,
        "class_id": class_id,
        "paper_id": paper_id,
        "audit_id": audit_id,
        "audit_version": audit_version,
        "issued_at": payload["issued_at"],
    }
    payload_without_version = {
        **payload,
        "audit_version": None,
    }
    expected_without_version = {
        **expected,
        "audit_version": None,
    }
    if not hmac.compare_digest(
        _canonical_payload(payload_without_version),
        _canonical_payload(expected_without_version),
    ):
        _invalid_token()
    if payload["audit_version"] != audit_version:
        raise DomainError(
            code="REVIEW_VERSION_CONFLICT",
            module="m0",
            message="the score audit changed before review submission",
            recoverable=True,
        )
    return dict(payload)


def flow_issued_at(payload: Mapping[str, object]) -> datetime:
    """Return the signed, canonical UTC time used by submission contracts."""

    validated = _validated_payload(dict(payload))
    return _parse_utc(validated["issued_at"])


def stable_flow_identifier(prefix: str, token: str) -> str:
    """Derive a replay-stable opaque identifier from a verified token."""

    if (
        not _IDENTIFIER.fullmatch(prefix)
        or not isinstance(token, str)
        or not token
        or len(token) > 2_048
    ):
        raise ValueError("flow identity input is invalid")
    from django.conf import settings

    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        f"{prefix}\0{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{prefix}_{digest}"


def _validated_payload(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _TOKEN_KEYS:
        _invalid_token()
    payload = cast(dict[str, object], value)
    if (
        payload["v"] != 1
        or payload["purpose"] not in _PURPOSES
        or not _valid_identifier(payload["actor_id"])
        or not _valid_identifier(payload["course_id"])
        or not _valid_identifier(payload["class_id"])
        or not _valid_optional_identifier(payload["paper_id"])
        or not _valid_optional_identifier(payload["audit_id"])
        or (
            payload["audit_version"] is not None
            and (
                isinstance(payload["audit_version"], bool)
                or not isinstance(payload["audit_version"], int)
                or payload["audit_version"] < 1
            )
        )
        or not _valid_issued_at(payload["issued_at"])
    ):
        _invalid_token()
    return dict(payload)


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _valid_optional_identifier(value: object) -> bool:
    return value is None or _valid_identifier(value)


def _canonical_payload(value: dict[str, object]) -> bytes:
    ordered = tuple((key, value[key]) for key in sorted(_TOKEN_KEYS))
    return repr(ordered).encode("utf-8")


def _valid_issued_at(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = _parse_utc(value)
    except (TypeError, ValueError):
        return False
    return value == _format_utc(parsed)


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("flow time is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("flow time must be timezone aware")
    normalized = parsed.astimezone(timezone.utc)
    if normalized.utcoffset() != timezone.utc.utcoffset(normalized):
        raise ValueError("flow time must be UTC")
    return normalized


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _invalid_token() -> None:
    raise DomainError(
        code="WEB_FLOW_TOKEN_INVALID",
        module="m0",
        message="the Web workflow token is invalid or expired",
        recoverable=True,
    )
