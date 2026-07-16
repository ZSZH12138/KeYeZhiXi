"""M0 repository boundary for persisted learning events."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.events import EventAck, LearningEvent


_EVENT_TABLE = "m0_learning_events"


class M0Repository(Protocol):
    """Persistence operations owned exclusively by M0."""

    def append_event(self, event: LearningEvent) -> EventAck:
        """Persist one replay-safe event and acknowledge its outcome."""

    def get_event(self, event_id: str) -> LearningEvent | None:
        """Load one event by its idempotent identity."""
