"""M0 learning-event contracts owned by 陈."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


def _copy_json_value(value: Any, *, active_container_ids: set[int]) -> Any:
    """Rebuild the lossless JSON subset accepted for event payloads."""

    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if type(value) not in {list, dict}:
        raise ValueError("event payloads accept only lossless JSON values")

    container_id = id(value)
    if container_id in active_container_ids:
        raise ValueError("event payloads must not contain recursive containers")
    active_container_ids.add(container_id)
    try:
        if type(value) is list:
            return [
                _copy_json_value(item, active_container_ids=active_container_ids)
                for item in value
            ]
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return {
            key: _copy_json_value(item, active_container_ids=active_container_ids)
            for key, item in value.items()
        }
    finally:
        active_container_ids.remove(container_id)


def _ensure_lossless_json(value: Any) -> None:
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


class LearningEvent(ContractModel):
    """One pseudonymous, replay-safe learning event consumed by M0."""

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    attempt_id: str | None = Field(min_length=1)
    payload: dict[str, Any]
    occurred_at: datetime

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_and_copy_payload(cls, value: Any) -> dict[str, Any]:
        copied = _copy_json_value(value, active_container_ids=set())
        if type(copied) is not dict:
            raise ValueError("payload must be a JSON object")
        _ensure_lossless_json(copied)
        return copied

    def idempotency_key(self) -> str:
        """Return a stable content key for duplicate-event detection."""

        return self.event_id


class EventAck(ContractModel):
    """Persistence acknowledgement partitioned by event outcome."""

    accepted_event_ids: list[str]
    duplicate_event_ids: list[str]
    failed_event_ids: list[str]
    persisted_at: datetime

    def validate_business_rules(self) -> None:
        """Require unique identifiers and mutually exclusive outcome sets."""

        partitions = (
            self.accepted_event_ids,
            self.duplicate_event_ids,
            self.failed_event_ids,
        )
        flattened = [event_id for partition in partitions for event_id in partition]
        if any(not event_id for event_id in flattened) or any(
            len(partition) != len(set(partition)) for partition in partitions
        ) or len(flattened) != len(set(flattened)):
            raise DomainError(
                code="EVENT_ACK_PARTITION_INVALID",
                module="m0",
                message="event acknowledgement outcomes must form disjoint ID sets",
            )

    def all_succeeded(self) -> bool:
        """Return whether persistence reported no failed events."""

        return not self.failed_event_ids

    def accepted_count(self) -> int:
        """Return the number of newly persisted events."""

        return len(self.accepted_event_ids)
