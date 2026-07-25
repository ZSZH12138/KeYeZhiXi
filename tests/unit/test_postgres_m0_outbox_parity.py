"""SQLite-parity checks for the PostgreSQL M0 compatibility outbox."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
from typing import Iterator

import pytest

from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.postgresql import m0_outbox_repository
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
)
from course_insight.modules.m0_platform.outbox import OutboxRecord


OCCURRED_AT = datetime(2025, 1, 2, tzinfo=timezone.utc)
ENQUEUED_AT = datetime(2026, 7, 25, tzinfo=timezone.utc)


class _Cursor:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row
        self.rowcount = 1 if row is not None else -1

    def fetchone(self) -> dict[str, object] | None:
        return self._row


class _CaptureConnection:
    def __init__(self, event_id: str) -> None:
        self._event_id = event_id
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        normalized = " ".join(statement.split())
        self.calls.append((normalized, tuple(parameters)))
        if "INSERT INTO m0_learning_events" in normalized:
            return _Cursor({"event_id": self._event_id})
        return _Cursor()


def _event(event_id: str) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        event_type="assessment_completed",
        course_id="course-1",
        class_id="class-1",
        learner_id="learner-1",
        attempt_id="attempt-1",
        payload={"score": 7},
        occurred_at=OCCURRED_AT,
    )


def _claimed_record(event: LearningEvent) -> OutboxRecord:
    return OutboxRecord(
        event_id=event.event_id,
        serialized_record=dumps_json(event.to_dict()),
        status="processing",
        attempt_count=1,
        version=2,
        available_at=ENQUEUED_AT,
        locked_by="compat-worker",
        locked_at=ENQUEUED_AT,
        lease_until=ENQUEUED_AT + timedelta(seconds=30),
        last_error_code=None,
        created_at=ENQUEUED_AT,
        updated_at=ENQUEUED_AT,
    )


def test_append_stamps_outbox_with_enqueue_clock_not_event_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("event-old")
    pool = object()
    connection = _CaptureConnection(event.event_id)

    @contextmanager
    def transaction(candidate: object) -> Iterator[_CaptureConnection]:
        assert candidate is pool
        yield connection

    monkeypatch.setattr(m0_outbox_repository, "m0_transaction", transaction)
    repository = PostgresM0Repository(
        pool,  # type: ignore[arg-type]
        outbox_clock=lambda: ENQUEUED_AT,
    )

    assert repository.append_events([event]) == ((event.event_id,), ())

    event_parameters = connection.calls[0][1]
    outbox_parameters = connection.calls[1][1]
    assert event_parameters[3] == OCCURRED_AT
    assert outbox_parameters[2:] == (
        ENQUEUED_AT,
        ENQUEUED_AT,
        ENQUEUED_AT,
    )


def test_compatibility_delivery_never_strands_unattempted_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = deque(
        _claimed_record(_event(f"event-{index}"))
        for index in range(1, 4)
    )
    repository = PostgresM0Repository(
        object(),  # type: ignore[arg-type]
        outbox_clock=lambda: ENQUEUED_AT,
    )
    batch_sizes: list[int] = []
    claimed: list[str] = []
    terminal: list[str] = []

    def claim(
        worker_id: str,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> tuple[OutboxRecord, ...]:
        del worker_id, now, lease_until
        batch_sizes.append(batch_size)
        batch = tuple(
            records.popleft()
            for _ in range(min(batch_size, len(records)))
        )
        claimed.extend(record.event_id for record in batch)
        return batch

    def acknowledge(
        worker_id: str,
        references: list[tuple[str, int]],
    ) -> tuple[str, ...]:
        del worker_id
        event_ids = tuple(event_id for event_id, _ in references)
        terminal.extend(event_ids)
        return event_ids

    def fail(
        worker_id: str,
        event_id: str,
        **kwargs: object,
    ) -> bool:
        del worker_id, kwargs
        terminal.append(event_id)
        return True

    monkeypatch.setattr(repository, "claim_outbox_batch", claim)
    monkeypatch.setattr(repository, "mark_outbox_delivered", acknowledge)
    monkeypatch.setattr(repository, "mark_outbox_failed", fail)

    def callback(event_id: str, record: str) -> None:
        del record
        if event_id == "event-2":
            raise RuntimeError("delivery failed")

    with pytest.raises(RuntimeError, match="delivery failed"):
        repository.deliver_outbox_records(callback)

    assert batch_sizes == [1, 1]
    assert claimed == terminal == ["event-1", "event-2"]
    assert [record.event_id for record in records] == ["event-3"]


def test_compatibility_delivery_renews_during_slow_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("event-slow")
    records = deque((_claimed_record(event),))
    repository = PostgresM0Repository(
        object(),  # type: ignore[arg-type]
        outbox_clock=lambda: ENQUEUED_AT,
    )
    monkeypatch.setattr(
        repository,
        "_compat_lease_seconds",
        0.2,
        raising=False,
    )
    monkeypatch.setattr(
        repository,
        "_compat_heartbeat_interval_seconds",
        0.01,
        raising=False,
    )
    renewal_count = 0
    acknowledged: list[str] = []
    renewed_twice = threading.Event()

    def claim(*args: object, **kwargs: object) -> tuple[OutboxRecord, ...]:
        del args, kwargs
        return (records.popleft(),) if records else ()

    def renew(*args: object, **kwargs: object) -> tuple[str, ...]:
        nonlocal renewal_count
        del args, kwargs
        renewal_count += 1
        if renewal_count >= 2:
            renewed_twice.set()
        return (event.event_id,)

    def acknowledge(
        worker_id: str,
        references: list[tuple[str, int]],
    ) -> tuple[str, ...]:
        del worker_id
        acknowledged.extend(event_id for event_id, _ in references)
        return tuple(acknowledged)

    monkeypatch.setattr(repository, "claim_outbox_batch", claim)
    monkeypatch.setattr(repository, "renew_outbox_leases", renew)
    monkeypatch.setattr(repository, "mark_outbox_delivered", acknowledge)

    def callback(event_id: str, serialized_record: str) -> None:
        del event_id, serialized_record
        assert renewed_twice.wait(timeout=1)

    repository.deliver_outbox_records(callback)

    assert renewal_count >= 2
    assert acknowledged == [event.event_id]


@pytest.mark.parametrize(
    ("callback_error", "expected_error"),
    (
        (None, RuntimeError),
        (OSError("callback failed after lease loss"), OSError),
    ),
)
def test_compatibility_delivery_never_writes_terminal_state_after_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
    callback_error: OSError | None,
    expected_error: type[Exception],
) -> None:
    event = _event("event-stale")
    records = deque((_claimed_record(event),))
    repository = PostgresM0Repository(
        object(),  # type: ignore[arg-type]
        outbox_clock=lambda: ENQUEUED_AT,
    )
    monkeypatch.setattr(
        repository,
        "_compat_heartbeat_interval_seconds",
        0.01,
        raising=False,
    )
    terminal_calls: list[str] = []
    lease_lost = threading.Event()

    def claim(*args: object, **kwargs: object) -> tuple[OutboxRecord, ...]:
        del args, kwargs
        return (records.popleft(),) if records else ()

    monkeypatch.setattr(repository, "claim_outbox_batch", claim)
    def lose_lease(*args: object, **kwargs: object) -> tuple[()]:
        del args, kwargs
        lease_lost.set()
        return ()

    monkeypatch.setattr(repository, "renew_outbox_leases", lose_lease)
    monkeypatch.setattr(
        repository,
        "mark_outbox_delivered",
        lambda *args, **kwargs: terminal_calls.append("ack"),
    )
    monkeypatch.setattr(
        repository,
        "mark_outbox_failed",
        lambda *args, **kwargs: terminal_calls.append("fail"),
    )

    def callback(event_id: str, serialized_record: str) -> None:
        del event_id, serialized_record
        assert lease_lost.wait(timeout=1)
        if callback_error is not None:
            raise callback_error

    with pytest.raises(expected_error) as captured:
        repository.deliver_outbox_records(callback)

    if callback_error is None:
        assert "lease was lost" in str(captured.value)
    else:
        assert captured.value is callback_error
    assert terminal_calls == []
