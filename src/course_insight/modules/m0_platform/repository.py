"""M0 repository boundary for persisted learning events."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Callable, Protocol

from course_insight.contracts.events import LearningEvent


class M0Repository(Protocol):
    """Persistence operations owned exclusively by M0."""

    def initialize(self) -> None:
        """Create or migrate the repository storage."""

    def append_events(
        self,
        events: Sequence[LearningEvent],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Atomically persist events and their outbox records.

        Return newly accepted identifiers followed by identifiers already
        present in storage. Repeated identifiers within one input batch are
        collapsed so acknowledgement partitions remain disjoint.
        """

    def deliver_outbox_records(
        self,
        deliver: Callable[[str, str], None],
    ) -> None:
        """Deliver and delete pending records under one repository lock."""

    def schema_is_current(self) -> bool:
        """Return whether storage has the application schema version."""
