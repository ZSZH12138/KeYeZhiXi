"""Private M7/M8 validation helpers shared by 童 and 冯."""

from __future__ import annotations

import json
import math
from typing import Any, Protocol, TypeVar

from course_insight.contracts.errors import DomainError


SCORE_TOLERANCE = 1e-9
COMPLETED_REVIEW_STATUSES = frozenset(
    {"approved", "completed", "confirmed", "not_required", "reviewed"}
)


class _AuditRecordLike(Protocol):
    audit_id: str
    item_instance_id: str
    audit_version: int
    scoring_method: str


class _LearningEventLike(Protocol):
    learner_id: str
    attempt_id: str | None


_AuditRecordT = TypeVar("_AuditRecordT", bound=_AuditRecordLike)


def finite_score_sum(
    values: list[float],
    *,
    code: str,
    message: str,
    details: dict[str, Any],
) -> float:
    """Sum finite fields while translating aggregate overflow consistently."""

    try:
        total = math.fsum(values)
    except (OverflowError, ValueError):
        total = None
    if total is None or not math.isfinite(total):
        raise DomainError(
            code=code,
            module="m8",
            message=message,
            details=details,
        ) from None
    return total


def copy_json_value(value: Any, *, active_container_ids: set[int]) -> Any:
    """Rebuild one value from the lossless JSON subset accepted at boundaries."""

    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if type(value) not in {list, dict}:
        raise ValueError("parameters accept only lossless JSON values")

    container_id = id(value)
    if container_id in active_container_ids:
        raise ValueError("parameters must not contain recursive containers")
    active_container_ids.add(container_id)
    try:
        if type(value) is list:
            return [
                copy_json_value(item, active_container_ids=active_container_ids)
                for item in value
            ]
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {
            key: copy_json_value(item, active_container_ids=active_container_ids)
            for key, item in value.items()
        }
    finally:
        active_container_ids.remove(container_id)


def ensure_lossless_json(value: Any) -> None:
    """Reject values that cannot survive UTF-8 stdlib JSON serialization."""

    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        serialized.encode("utf-8")
        restored = json.loads(serialized)
    except (OverflowError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("value must roundtrip through UTF-8 JSON") from error
    if restored != value:
        raise ValueError("value must roundtrip through UTF-8 JSON")


def require_unique(
    values: list[Any],
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Raise one deterministic M8 error for duplicated identifiers."""

    if len(values) != len(set(values)):
        raise DomainError(
            code=code,
            module="m8",
            message=message,
            details=details,
        )


def latest_audit_records(records: list[_AuditRecordT]) -> list[_AuditRecordT]:
    """Select one highest version for every stable audit/item identity."""

    latest: dict[tuple[str, str], _AuditRecordT] = {}
    for record in records:
        key = (record.audit_id, record.item_instance_id)
        current = latest.get(key)
        if current is None or record.audit_version > current.audit_version:
            latest[key] = record
    return list(latest.values())


def validate_audit_history(records: list[_AuditRecordT]) -> None:
    """Require stable item identity and complete teacher-review versions."""

    audit_to_item: dict[str, str] = {}
    item_to_audit: dict[str, str] = {}
    versions_by_audit: dict[str, list[_AuditRecordT]] = {}
    for record in records:
        known_item = audit_to_item.setdefault(record.audit_id, record.item_instance_id)
        known_audit = item_to_audit.setdefault(record.item_instance_id, record.audit_id)
        if known_item != record.item_instance_id or known_audit != record.audit_id:
            raise DomainError(
                code="AUDIT_REFERENCE_MISMATCH",
                module="m8",
                message="audit and item identifiers must retain one stable mapping",
            )
        versions_by_audit.setdefault(record.audit_id, []).append(record)

    for audit_id, audit_records in versions_by_audit.items():
        versions = sorted(record.audit_version for record in audit_records)
        if versions != list(range(1, versions[-1] + 1)):
            raise DomainError(
                code="AUDIT_VERSION_SEQUENCE_INVALID",
                module="m8",
                message="audit history must retain every version starting at one",
                details={"audit_id": audit_id, "versions": versions},
            )
        if any(
            record.audit_version > 1 and record.scoring_method != "teacher_override"
            for record in audit_records
        ):
            raise DomainError(
                code="REVIEW_VERSION_CONFLICT",
                module="m8",
                message="audit revisions must be teacher overrides",
                details={"audit_id": audit_id},
                recoverable=True,
            )


def validate_learning_event_references(
    events: list[_LearningEventLike],
    *,
    attempt_id: str,
    learner_id: str,
) -> None:
    """Keep attempt-scoped events aligned with the scoring bundle identity."""

    if any(
        event.learner_id != learner_id
        or (event.attempt_id is not None and event.attempt_id != attempt_id)
        for event in events
    ):
        raise DomainError(
            code="LEARNING_EVENT_REFERENCE_MISMATCH",
            module="m8",
            message="learning events must reference the bundle learner and attempt",
        )
