"""Explicit compatibility façade over the permanent leased outbox."""

from __future__ import annotations

from pathlib import Path

from course_insight.modules.m0_platform.outbox import IdempotentJsonlSink
from course_insight.modules.m0_platform.repository import M0Repository


class M0EventStore:
    """Deliver pending outbox records to the durable JSONL audit log."""

    def __init__(self, repository: M0Repository, audit_log_path: Path) -> None:
        self._repository = repository
        self._sink = IdempotentJsonlSink(audit_log_path)

    def deliver_pending(self) -> None:
        """Deliver pending records idempotently and acknowledge them in SQLite."""

        def deliver(event_id: str, serialized_record: str) -> None:
            self._sink.append_if_absent(event_id, serialized_record)

        self._repository.deliver_outbox_records(deliver)
