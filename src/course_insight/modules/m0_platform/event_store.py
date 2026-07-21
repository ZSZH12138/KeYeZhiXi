"""M0 event-outbox delivery without database implementation details."""

from __future__ import annotations

import json
from pathlib import Path

from course_insight.infrastructure.logging import append_json_log
from course_insight.modules.m0_platform.repository import M0Repository


class M0EventStore:
    """Deliver pending outbox records to the durable JSONL audit log."""

    def __init__(self, repository: M0Repository, audit_log_path: Path) -> None:
        self._repository = repository
        self._audit_log_path = audit_log_path

    def deliver_pending(self) -> None:
        """Deliver pending records idempotently and acknowledge them in SQLite."""

        logged_event_ids: set[str] | None = None

        def deliver(event_id: str, serialized_record: str) -> None:
            nonlocal logged_event_ids
            if logged_event_ids is None:
                logged_event_ids = self._logged_event_ids()
            record = json.loads(serialized_record)
            if type(record) is not dict or record.get("event_id") != event_id:
                raise ValueError("event outbox record is invalid")
            if event_id not in logged_event_ids:
                append_json_log(self._audit_log_path, record)
                logged_event_ids.add(event_id)

        self._repository.deliver_outbox_records(deliver)

    def _logged_event_ids(self) -> set[str]:
        if not self._audit_log_path.exists():
            return set()
        event_ids: set[str] = set()
        for line in self._audit_log_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if type(record) is not dict or not isinstance(record.get("event_id"), str):
                raise ValueError("learning event audit log contains an invalid row")
            event_ids.add(record["event_id"])
        return event_ids
