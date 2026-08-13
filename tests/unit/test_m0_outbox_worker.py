from __future__ import annotations

import json
import logging
import signal
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.config import OutboxSettings
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite import SCHEMA_VERSION, connect_sqlite, migrate
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.workflow_migration import (
    ASSESSMENT_RUNS_SUBMIT_INDEX_SQL,
    ASSESSMENT_RUNS_V6_SQL,
)
from course_insight.modules.m0_platform.outbox import (
    IdempotentJsonlSink,
    OutboxRecord,
)
from course_insight.modules.m0_platform.outbox_worker import OutboxWorker


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _event(
    index: int,
    *,
    occurred_at: datetime = NOW,
    event_id: str | None = None,
) -> LearningEvent:
    return LearningEvent(
        event_id=f"event_{index}" if event_id is None else event_id,
        event_type="assessment_scored",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        attempt_id="attempt_1",
        payload={"score": index},
        occurred_at=occurred_at,
    )


def _repository(tmp_path: Path) -> SQLiteM0Repository:
    repository = SQLiteM0Repository(
        tmp_path / "runtime" / "app.sqlite3",
        outbox_clock=lambda: NOW,
    )
    repository.initialize()
    return repository


def _settings(**updates: object) -> OutboxSettings:
    values: dict[str, object] = {
        "batch_size": 10,
        "poll_interval_seconds": 0.01,
        "lease_seconds": 30,
        "max_retries": 3,
        "retry_base_seconds": 1,
        "retry_max_seconds": 10,
        "retry_jitter_ratio": 0,
        "heartbeat_interval_seconds": 5,
        **updates,
    }
    return OutboxSettings(**values)


def _restore_v6_workflow_schema(connection: sqlite3.Connection) -> None:
    """Make a current empty database accurately represent the v6 workflow DDL."""

    connection.execute("DROP TABLE m0_assessment_runs")
    connection.execute(ASSESSMENT_RUNS_V6_SQL)
    connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)


def test_outbox_record_is_private_immutable_and_rejects_invalid_state() -> None:
    serialized = dumps_json({"event_id": "event_1", "value": 1})
    record = OutboxRecord(
        event_id="event_1",
        serialized_record=serialized,
        status="pending",
        attempt_count=0,
        version=1,
        available_at=NOW,
        locked_by=None,
        locked_at=None,
        lease_until=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )

    with pytest.raises(Exception):
        record.status = "dead"  # type: ignore[misc]
    with pytest.raises(ValueError):
        OutboxRecord(
            event_id="event_1",
            serialized_record=serialized,
            status="processing",
            attempt_count=1,
            version=2,
            available_at=NOW,
            locked_by=None,
            locked_at=NOW,
            lease_until=NOW + timedelta(seconds=30),
            last_error_code=None,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValueError):
        OutboxRecord(
            event_id="event_1",
            serialized_record='{"event_id":"different"}',
            status="pending",
            attempt_count=0,
            version=1,
            available_at=NOW,
            locked_by=None,
            locked_at=None,
            lease_until=None,
            last_error_code=None,
            created_at=NOW,
            updated_at=NOW,
        )


def test_claim_orders_available_rows_and_reclaims_only_expired_leases(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events(
        [
            _event(2, occurred_at=NOW - timedelta(seconds=1)),
            _event(1, occurred_at=NOW - timedelta(seconds=2)),
            _event(3, occurred_at=NOW + timedelta(seconds=1)),
        ]
    )
    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        connection.execute(
            """
            UPDATE m0_event_outbox SET available_at = ?
            WHERE event_id = 'event_1'
            """,
            ((NOW - timedelta(seconds=2)).isoformat(),),
        )
        connection.execute(
            """
            UPDATE m0_event_outbox SET available_at = ?
            WHERE event_id = 'event_2'
            """,
            ((NOW - timedelta(seconds=1)).isoformat(),),
        )
        connection.execute(
            """
            UPDATE m0_event_outbox SET available_at = ?
            WHERE event_id = 'event_3'
            """,
            ((NOW + timedelta(seconds=1)).isoformat(),),
        )

    first = repository.claim_outbox_batch(
        "worker_a",
        now=NOW,
        lease_until=NOW + timedelta(seconds=10),
        batch_size=10,
    )
    blocked = repository.claim_outbox_batch(
        "worker_b",
        now=NOW + timedelta(seconds=5),
        lease_until=NOW + timedelta(seconds=15),
        batch_size=10,
    )
    reclaimed = repository.claim_outbox_batch(
        "worker_b",
        now=NOW + timedelta(seconds=11),
        lease_until=NOW + timedelta(seconds=21),
        batch_size=10,
    )

    assert [record.event_id for record in first] == ["event_1", "event_2"]
    assert [record.event_id for record in blocked] == ["event_3"]
    assert [record.event_id for record in reclaimed] == ["event_1", "event_2"]
    assert all(record.attempt_count == 2 for record in reclaimed)


def test_future_domain_occurrence_does_not_delay_new_outbox_delivery(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events(
        [_event(1, occurred_at=NOW + timedelta(days=30))]
    )

    claimed = repository.claim_outbox_batch(
        "worker_a",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        batch_size=1,
    )

    assert [record.event_id for record in claimed] == ["event_1"]
    assert claimed[0].available_at == NOW
    assert json.loads(claimed[0].serialized_record)["occurred_at"].startswith(
        "2026-08-24"
    )


def test_ack_fail_dead_and_renew_are_worker_and_version_cas(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1), _event(2), _event(3)])
    claimed = repository.claim_outbox_batch(
        "worker_a",
        now=NOW,
        lease_until=NOW + timedelta(seconds=10),
        batch_size=3,
    )

    renewed = repository.renew_outbox_leases(
        "worker_a",
        [(claimed[0].event_id, claimed[0].version)],
        now=NOW + timedelta(seconds=1),
        lease_until=NOW + timedelta(seconds=20),
    )
    not_renewed = repository.renew_outbox_leases(
        "worker_b",
        [(claimed[0].event_id, claimed[0].version)],
        now=NOW + timedelta(seconds=1),
        lease_until=NOW + timedelta(seconds=20),
    )
    stale_ack = repository.mark_outbox_delivered(
        "worker_a",
        [(claimed[0].event_id, claimed[0].version - 1)],
    )
    acked = repository.mark_outbox_delivered(
        "worker_a",
        [(claimed[0].event_id, claimed[0].version)],
    )
    pending = repository.mark_outbox_failed(
        "worker_a",
        claimed[1].event_id,
        expected_version=claimed[1].version,
        now=NOW,
        next_attempt_at=NOW + timedelta(seconds=30),
        error_code="OUTBOX_SINK_UNAVAILABLE",
        dead=False,
    )
    dead = repository.mark_outbox_failed(
        "worker_a",
        claimed[2].event_id,
        expected_version=claimed[2].version,
        now=NOW,
        next_attempt_at=NOW,
        error_code="OUTBOX_RECORD_INVALID",
        dead=True,
    )

    assert renewed == ("event_1",)
    assert not_renewed == ()
    assert stale_ack == ()
    assert acked == ("event_1",)
    assert pending is True
    assert dead is True
    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        rows = connection.execute(
            """
            SELECT event_id, status, last_error_code
            FROM m0_event_outbox
            ORDER BY event_id
            """
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("event_2", "pending", "OUTBOX_SINK_UNAVAILABLE"),
        ("event_3", "dead", "OUTBOX_RECORD_INVALID"),
    ]


def test_idempotent_jsonl_sink_rejects_corruption_and_record_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit" / "events.jsonl"
    sink = IdempotentJsonlSink(path)
    serialized = dumps_json({"event_id": "event_1", "value": 1})

    assert sink.append_if_absent("event_1", serialized) is True
    assert sink.append_if_absent("event_1", serialized) is False
    assert path.read_text(encoding="utf-8").splitlines() == [serialized]
    with pytest.raises(ValueError):
        sink.append_if_absent(
            "event_1",
            dumps_json({"event_id": "event_1", "value": 2}),
        )

    path.write_text("{broken\n", encoding="utf-8")
    with pytest.raises(ValueError):
        sink.append_if_absent("event_2", dumps_json({"event_id": "event_2"}))


def test_worker_once_delivers_last_batch_and_writes_payload_free_status(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1), _event(2)])
    status_path = tmp_path / "runtime" / "outbox_worker" / "status.json"
    worker = OutboxWorker(
        repository=repository,
        sink=IdempotentJsonlSink(tmp_path / "runtime" / "audit" / "events.jsonl"),
        settings=_settings(),
        status_path=status_path,
        worker_id="worker_a",
        clock=lambda: NOW,
        jitter=lambda: 0.5,
    )

    snapshot = worker.run(once=True)

    assert snapshot.state == "stopped"
    assert snapshot.claimed_count == 2
    assert snapshot.delivered_count == 2
    assert snapshot.dead_count == 0
    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        assert connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0] == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert set(status) == {
        "worker_id",
        "state",
        "last_heartbeat_at",
        "last_success_at",
        "last_error_code",
        "claimed_count",
        "delivered_count",
        "dead_count",
    }
    rendered = status_path.read_text(encoding="utf-8")
    assert "score" not in rendered
    assert str(tmp_path) not in rendered


def test_worker_restart_after_sink_write_before_ack_is_idempotent(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events(
        [_event(1, occurred_at=NOW - timedelta(days=1))]
    )
    sink = IdempotentJsonlSink(tmp_path / "audit" / "events.jsonl")
    claimed = repository.claim_outbox_batch(
        "crashed_worker",
        now=NOW,
        lease_until=NOW + timedelta(seconds=1),
        batch_size=1,
    )
    assert sink.append_if_absent(
        claimed[0].event_id,
        claimed[0].serialized_record,
    )
    worker = OutboxWorker(
        repository=repository,
        sink=sink,
        settings=_settings(),
        status_path=tmp_path / "status.json",
        worker_id="restarted_worker",
        clock=lambda: NOW + timedelta(seconds=2),
        jitter=lambda: 0.5,
    )

    snapshot = worker.run(once=True)

    assert snapshot.delivered_count == 1
    assert (tmp_path / "audit" / "events.jsonl").read_text(
        encoding="utf-8"
    ).count("\n") == 1


class _SelectiveSink:
    def __init__(self, failing_ids: set[str]) -> None:
        self.failing_ids = failing_ids
        self.delivered: list[str] = []

    def append_if_absent(self, event_id: str, serialized_record: str) -> bool:
        del serialized_record
        if event_id in self.failing_ids:
            raise OSError("unsafe details must not escape")
        self.delivered.append(event_id)
        return True


def test_worker_continues_after_partial_failure_and_dead_letters_at_limit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1), _event(2)])
    sink = _SelectiveSink({"event_1"})
    worker = OutboxWorker(
        repository=repository,
        sink=sink,
        settings=_settings(max_retries=1),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: NOW,
        jitter=lambda: 0.5,
        logger=logging.getLogger("outbox-test"),
    )

    snapshot = worker.run(once=True)

    assert sink.delivered == ["event_2"]
    assert snapshot.delivered_count == 1
    assert snapshot.dead_count == 1
    assert snapshot.last_error_code == "OUTBOX_SINK_UNAVAILABLE"
    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        row = connection.execute(
            """
            SELECT status, attempt_count, last_error_code
            FROM m0_event_outbox
            WHERE event_id = 'event_1'
            """
        ).fetchone()
    assert tuple(row) == ("dead", 1, "OUTBOX_SINK_UNAVAILABLE")


def test_worker_stop_is_cooperative_and_empty_once_never_sleeps(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    sleeps: list[float] = []
    worker = OutboxWorker(
        repository=repository,
        sink=IdempotentJsonlSink(tmp_path / "events.jsonl"),
        settings=_settings(),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: NOW,
        sleeper=sleeps.append,
        jitter=lambda: 0.5,
    )
    worker.request_stop()

    snapshot = worker.run(once=True)

    assert snapshot.state == "stopped"
    assert sleeps == []


def test_explicit_compatibility_delivery_uses_claim_sink_ack_boundaries(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events(
        [_event(1, occurred_at=NOW - timedelta(days=1))]
    )
    delivered: list[str] = []

    repository.deliver_outbox_records(
        lambda event_id, serialized: delivered.append(
            f"{event_id}:{json.loads(serialized)['event_id']}"
        )
    )

    assert delivered == ["event_1:event_1"]
    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        assert connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0] == 0


def test_compatibility_callback_failure_releases_record_for_retry(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events(
        [_event(1, occurred_at=NOW - timedelta(days=1))]
    )

    with pytest.raises(OSError):
        repository.deliver_outbox_records(
            lambda event_id, serialized: (_ for _ in ()).throw(
                OSError("callback failed")
            )
        )

    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        row = connection.execute(
            """
            SELECT status, attempt_count, version, locked_by, last_error_code
            FROM m0_event_outbox
            """
        ).fetchone()
    assert tuple(row) == (
        "pending",
        1,
        3,
        None,
        "OUTBOX_DELIVERY_FAILED",
    )


def test_compatibility_delivery_renews_lease_during_slow_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1)])
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
    original_renew = repository.renew_outbox_leases
    renewal_count = 0
    renewed_twice = threading.Event()

    def renew(*args: object, **kwargs: object) -> tuple[str, ...]:
        nonlocal renewal_count
        renewal_count += 1
        if renewal_count >= 2:
            renewed_twice.set()
        return original_renew(*args, **kwargs)

    monkeypatch.setattr(repository, "renew_outbox_leases", renew)

    def slow_callback(event_id: str, serialized_record: str) -> None:
        del event_id, serialized_record
        assert renewed_twice.wait(timeout=1)

    repository.deliver_outbox_records(slow_callback)

    assert renewal_count >= 2


def test_compatibility_delivery_does_not_ack_after_lease_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1)])
    monkeypatch.setattr(
        repository,
        "_compat_heartbeat_interval_seconds",
        0.01,
        raising=False,
    )
    terminal_calls: list[str] = []
    lease_lost = threading.Event()

    def lose_lease(*args: object, **kwargs: object) -> tuple[()]:
        del args, kwargs
        lease_lost.set()
        return ()

    monkeypatch.setattr(
        repository,
        "renew_outbox_leases",
        lose_lease,
    )
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

    with pytest.raises(RuntimeError, match="lease was lost"):
        repository.deliver_outbox_records(callback)

    assert terminal_calls == []


def test_compatibility_delivery_preserves_callback_error_after_lease_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1)])
    monkeypatch.setattr(
        repository,
        "_compat_heartbeat_interval_seconds",
        0.01,
        raising=False,
    )
    terminal_calls: list[str] = []
    lease_lost = threading.Event()

    def lose_lease(*args: object, **kwargs: object) -> tuple[()]:
        del args, kwargs
        lease_lost.set()
        return ()

    monkeypatch.setattr(
        repository,
        "renew_outbox_leases",
        lose_lease,
    )
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
    callback_error = OSError("callback failed after lease loss")

    def failing_callback(event_id: str, serialized_record: str) -> None:
        del event_id, serialized_record
        assert lease_lost.wait(timeout=1)
        raise callback_error

    with pytest.raises(OSError) as captured:
        repository.deliver_outbox_records(failing_callback)

    assert captured.value is callback_error
    assert terminal_calls == []


def test_long_unicode_event_id_is_not_narrowed_by_private_outbox(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    event_id = "事件-" + ("长" * 300)
    repository.append_events([_event(1, event_id=event_id)])

    claimed = repository.claim_outbox_batch(
        "worker_a",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        batch_size=1,
    )
    sink = IdempotentJsonlSink(tmp_path / "events.jsonl")

    assert claimed[0].event_id == event_id
    assert sink.append_if_absent(
        claimed[0].event_id,
        claimed[0].serialized_record,
    )
    assert repository.mark_outbox_delivered(
        "worker_a",
        [(event_id, claimed[0].version)],
    ) == (event_id,)


def test_pending_failure_uses_bounded_exponential_backoff(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1)])
    worker = OutboxWorker(
        repository=repository,
        sink=_SelectiveSink({"event_1"}),
        settings=_settings(max_retries=3),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: NOW,
        jitter=lambda: 0.5,
    )

    worker.run(once=True)

    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        row = connection.execute(
            """
            SELECT status, available_at, updated_at
            FROM m0_event_outbox
            """
        ).fetchone()
    assert row["status"] == "pending"
    assert datetime.fromisoformat(row["available_at"]) == NOW + timedelta(seconds=1)
    assert datetime.fromisoformat(row["updated_at"]) == NOW


class _TransientRepository:
    def __init__(self, delegate: SQLiteM0Repository) -> None:
        self.delegate = delegate
        self.failures = 1

    def claim_outbox_batch(self, *args: object, **kwargs: object):
        if self.failures:
            self.failures -= 1
            raise OSError("database details must not escape")
        return self.delegate.claim_outbox_batch(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


def test_continuous_worker_recovers_after_temporary_database_failure(
    tmp_path: Path,
) -> None:
    delegate = _repository(tmp_path)
    delegate.append_events([_event(1)])
    repository = _TransientRepository(delegate)
    holder: dict[str, OutboxWorker] = {}
    sleep_count = 0

    def sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            holder["worker"].request_stop()

    worker = OutboxWorker(
        repository=repository,  # type: ignore[arg-type]
        sink=IdempotentJsonlSink(tmp_path / "events.jsonl"),
        settings=_settings(),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: NOW,
        sleeper=sleep,
        jitter=lambda: 0.5,
    )
    holder["worker"] = worker

    snapshot = worker.run()

    assert repository.failures == 0
    assert snapshot.delivered_count == 1
    assert snapshot.state == "stopped"
    assert sleep_count == 2


class _RenewCountingRepository:
    def __init__(self, delegate: SQLiteM0Repository) -> None:
        self.delegate = delegate
        self.renew_count = 0
        self._renewed = threading.Condition()

    def renew_outbox_leases(self, *args: object, **kwargs: object):
        renewed = self.delegate.renew_outbox_leases(*args, **kwargs)
        with self._renewed:
            self.renew_count += 1
            self._renewed.notify_all()
        return renewed

    def wait_for_renew_count(self, minimum: int, *, timeout: float) -> bool:
        with self._renewed:
            return self._renewed.wait_for(
                lambda: self.renew_count >= minimum,
                timeout=timeout,
            )

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)


class _BlockingSink:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()

    def append_if_absent(self, event_id: str, serialized_record: str) -> bool:
        del event_id, serialized_record
        self.started.set()
        try:
            self.release.wait()
            return True
        finally:
            self.completed.set()


def test_heartbeat_interval_renews_lease_during_slow_sink_io(
    tmp_path: Path,
) -> None:
    current = datetime.now(timezone.utc)
    delegate = SQLiteM0Repository(
        tmp_path / "runtime" / "app.sqlite3",
        outbox_clock=lambda: current,
    )
    delegate.initialize()
    delegate.append_events([_event(1, occurred_at=current)])
    repository = _RenewCountingRepository(delegate)
    sink = _BlockingSink()
    worker = OutboxWorker(
        repository=repository,  # type: ignore[arg-type]
        sink=sink,
        settings=_settings(
            lease_seconds=0.2,
            heartbeat_interval_seconds=0.02,
        ),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: datetime.now(timezone.utc),
        jitter=lambda: 0.5,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run, once=True)
        try:
            sink_started = sink.started.wait(timeout=1)
            if not sink_started and future.done():
                future.result()
            assert sink_started
            renewed_while_blocked = repository.wait_for_renew_count(
                2,
                timeout=1,
            )
        finally:
            sink.release.set()
        snapshot = future.result(timeout=1)

    assert renewed_while_blocked
    assert snapshot.delivered_count == 1
    assert repository.renew_count >= 2


def test_worker_stop_waits_for_in_flight_append_and_keeps_heartbeat_alive(
    tmp_path: Path,
) -> None:
    current = datetime.now(timezone.utc)
    delegate = SQLiteM0Repository(
        tmp_path / "runtime" / "app.sqlite3",
        outbox_clock=lambda: current,
    )
    delegate.initialize()
    delegate.append_events([_event(1, occurred_at=current)])
    repository = _RenewCountingRepository(delegate)
    sink = _BlockingSink()
    worker = OutboxWorker(
        repository=repository,  # type: ignore[arg-type]
        sink=sink,
        settings=_settings(
            lease_seconds=0.2,
            heartbeat_interval_seconds=0.02,
        ),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: datetime.now(timezone.utc),
        jitter=lambda: 0.5,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run)
        try:
            sink_started = sink.started.wait(timeout=1)
            if not sink_started and future.done():
                future.result()
            assert sink_started
            worker.request_stop()
            renewals_after_stop = repository.renew_count
            # Only one renewal can be in flight. Two later completions prove
            # that a fresh heartbeat cycle began after the stop request.
            renewals_continued_after_stop = repository.wait_for_renew_count(
                renewals_after_stop + 2,
                timeout=1,
            )
            still_running_while_blocked = not future.done()
            assert not sink.completed.is_set()
        finally:
            sink.release.set()
        snapshot = future.result(timeout=1)

    assert renewals_continued_after_stop
    assert still_running_while_blocked
    assert snapshot.state == "stopped"
    assert snapshot.delivered_count == 1


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_worker_logs_are_correlated_by_worker_id_without_event_payload(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append_events([_event(1)])
    handler = _CapturingHandler()
    logger = logging.getLogger("outbox-worker-context")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    worker = OutboxWorker(
        repository=repository,
        sink=_SelectiveSink({"event_1"}),
        settings=_settings(max_retries=1),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: NOW,
        jitter=lambda: 0.5,
        logger=logger,
    )

    worker.run(once=True)

    assert handler.records
    for record in handler.records:
        context = getattr(record, "_course_insight_context")
        assert context["worker_id"] == "worker_a"
        assert "event_id" not in vars(record)
        assert "serialized_record" not in vars(record)


def test_signal_handlers_are_restored_after_once_run(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    worker = OutboxWorker(
        repository=repository,
        sink=IdempotentJsonlSink(tmp_path / "events.jsonl"),
        settings=_settings(),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: NOW,
        jitter=lambda: 0.5,
    )
    before = {
        candidate: signal.getsignal(candidate)
        for candidate in (signal.SIGINT, signal.SIGTERM)
    }

    worker.run(once=True)

    after = {
        candidate: signal.getsignal(candidate)
        for candidate in (signal.SIGINT, signal.SIGTERM)
    }
    assert after == before


def test_long_poll_heartbeats_and_request_stop_interrupts_wait(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    worker = OutboxWorker(
        repository=repository,
        sink=IdempotentJsonlSink(tmp_path / "events.jsonl"),
        settings=_settings(
            poll_interval_seconds=30,
            lease_seconds=1,
            heartbeat_interval_seconds=0.05,
        ),
        status_path=tmp_path / "status.json",
        worker_id="worker_a",
        clock=lambda: datetime.now(timezone.utc),
        jitter=lambda: 0.5,
    )
    thread = threading.Thread(target=worker.run)
    thread.start()
    deadline = time.monotonic() + 2
    while worker.snapshot().state != "idle" and time.monotonic() < deadline:
        time.sleep(0.01)
    first_heartbeat = worker.snapshot().last_heartbeat_at
    time.sleep(0.12)
    later_heartbeat = worker.snapshot().last_heartbeat_at

    stopped_at = time.monotonic()
    worker.request_stop()
    thread.join(timeout=0.5)

    assert first_heartbeat is not None
    assert later_heartbeat is not None
    assert later_heartbeat > first_heartbeat
    assert not thread.is_alive()
    assert time.monotonic() - stopped_at < 0.5
    assert worker.snapshot().state == "stopped"


def test_v6_outbox_rows_migrate_losslessly_to_v7(tmp_path: Path) -> None:
    database_path = tmp_path / "app.sqlite3"
    legacy_event_id = "旧事件-" + ("长" * 300)
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        original = dumps_json(
            _event(1, event_id=legacy_event_id).to_dict()
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE m0_event_outbox")
        connection.execute(
            """
            CREATE TABLE m0_event_outbox (
                event_id TEXT PRIMARY KEY
                    REFERENCES m0_learning_events(event_id) ON DELETE CASCADE,
                record TEXT NOT NULL CHECK (
                    CASE WHEN json_valid(record)
                        THEN json(record) = record
                        ELSE 0
                    END
                )
            )
            """
        )
        connection.execute(
            """
            INSERT INTO m0_learning_events(
                event_id, idempotency_key, event_type, occurred_at, payload
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                legacy_event_id,
                "legacy_key",
                "assessment_scored",
                NOW.isoformat(),
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO m0_event_outbox(event_id, record) VALUES (?, ?)",
            (legacy_event_id, original),
        )
        _restore_v6_workflow_schema(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
        connection.execute("COMMIT")

        before_migration = datetime.now(timezone.utc)
        migrate(connection)
        after_migration = datetime.now(timezone.utc)
        row = connection.execute(
            """
            SELECT record, status, attempt_count, version, available_at,
                   locked_by, locked_at, lease_until, last_error_code
            FROM m0_event_outbox
            WHERE event_id = ?
            """,
            (legacy_event_id,),
        ).fetchone()
        versions = [
            int(item[0])
            for item in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    assert tuple(row[:4]) == (original, "pending", 0, 1)
    migrated_at = datetime.fromisoformat(str(row["available_at"]))
    assert before_migration <= migrated_at <= after_migration
    assert tuple(row[5:]) == (None, None, None, None)
    assert versions == list(range(1, SCHEMA_VERSION + 1))


def test_v6_future_domain_time_migrates_as_immediately_available(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "app.sqlite3"
    future = NOW + timedelta(days=30)
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        original = dumps_json(_event(1, occurred_at=future).to_dict())
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE m0_event_outbox")
        connection.execute(
            """
            CREATE TABLE m0_event_outbox (
                event_id TEXT PRIMARY KEY
                    REFERENCES m0_learning_events(event_id) ON DELETE CASCADE,
                record TEXT NOT NULL CHECK (
                    CASE WHEN json_valid(record)
                        THEN json(record) = record
                        ELSE 0
                    END
                )
            )
            """
        )
        connection.execute(
            """
            INSERT INTO m0_learning_events(
                event_id, idempotency_key, event_type, occurred_at, payload
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "event_1",
                "legacy_future_key",
                "assessment_scored",
                future.isoformat(),
                '{"score":1}',
            ),
        )
        connection.execute(
            "INSERT INTO m0_event_outbox(event_id, record) VALUES (?, ?)",
            ("event_1", original),
        )
        _restore_v6_workflow_schema(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
        connection.execute("COMMIT")

        before_migration = datetime.now(timezone.utc)
        migrate(connection)
        after_migration = datetime.now(timezone.utc)
        row = connection.execute(
            """
            SELECT available_at, created_at, updated_at, record
            FROM m0_event_outbox
            WHERE event_id = 'event_1'
            """
        ).fetchone()

    available_at = datetime.fromisoformat(str(row["available_at"]))
    assert before_migration <= available_at <= after_migration
    assert row["created_at"] == row["available_at"]
    assert row["updated_at"] == row["available_at"]
    assert json.loads(str(row["record"]))["occurred_at"].startswith(
        future.date().isoformat()
    )

    repository = SQLiteM0Repository(
        database_path,
        outbox_clock=lambda: after_migration,
    )
    claimed = repository.claim_outbox_batch(
        "worker_a",
        now=after_migration,
        lease_until=after_migration + timedelta(seconds=30),
        batch_size=1,
    )
    assert [record.event_id for record in claimed] == ["event_1"]


def test_non_contiguous_migration_ledger_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "app.sqlite3"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")

        with pytest.raises(
            RuntimeError,
            match="M0 workflow v5 schema is incompatible",
        ):
            migrate(connection)

        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    assert versions == [
        version
        for version in range(1, SCHEMA_VERSION + 1)
        if version != 6
    ]


def test_v7_migration_rejects_orphaned_legacy_outbox_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "app.sqlite3"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute("DROP TABLE m0_event_outbox")
        connection.execute(
            """
            CREATE TABLE m0_event_outbox (
                event_id TEXT PRIMARY KEY
                    REFERENCES m0_learning_events(event_id) ON DELETE CASCADE,
                record TEXT NOT NULL CHECK (
                    CASE WHEN json_valid(record)
                        THEN json(record) = record
                        ELSE 0
                    END
                )
            )
            """
        )
        connection.execute("DELETE FROM schema_migrations WHERE version >= 7")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO m0_event_outbox(event_id, record) VALUES (?, ?)",
            ("orphan_event", '{"event_id":"orphan_event"}'),
        )
        connection.execute("PRAGMA foreign_keys = ON")

        with pytest.raises(RuntimeError, match="foreign keys"):
            migrate(connection)

        assert connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0] == 1
