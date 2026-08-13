"""Permanent leased-outbox worker with bounded retries and safe observability."""

from __future__ import annotations

import logging
import math
import random
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol

from course_insight.infrastructure.config.models import OutboxSettings
from course_insight.infrastructure.json_io import write_json
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.infrastructure.logging import log_event
from course_insight.modules.m0_platform.outbox import (
    OutboxRecord,
    validate_outbox_identifier,
    validate_utc_datetime,
)
from course_insight.modules.m0_platform.repository import M0Repository


WorkerState = Literal["starting", "running", "idle", "stopping", "stopped"]


class OutboxSink(Protocol):
    def append_if_absent(
        self,
        event_id: str,
        serialized_record: str,
    ) -> bool:
        """Durably append a record, or return false when already present."""


@dataclass(frozen=True, slots=True)
class OutboxWorkerSnapshot:
    worker_id: str
    state: WorkerState
    last_heartbeat_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    claimed_count: int
    delivered_count: int
    dead_count: int

    def to_status_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "state": self.state,
            "last_heartbeat_at": _optional_time(self.last_heartbeat_at),
            "last_success_at": _optional_time(self.last_success_at),
            "last_error_code": self.last_error_code,
            "claimed_count": self.claimed_count,
            "delivered_count": self.delivered_count,
            "dead_count": self.dead_count,
        }


class OutboxWorker:
    """Claim briefly, deliver outside transactions, then acknowledge by CAS."""

    def __init__(
        self,
        *,
        repository: M0Repository,
        sink: OutboxSink,
        settings: OutboxSettings,
        status_path: Path,
        worker_id: str,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], object] | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        validate_outbox_identifier(worker_id, field="worker_id")
        self._repository = repository
        self._sink = sink
        self._settings = settings
        self._status_path = Path(status_path)
        self._logger = logger
        self._clock = _utc_now if clock is None else clock
        self._sleeper = sleeper
        self._jitter = random.random if jitter is None else jitter
        self._stop = threading.Event()
        self._snapshot = OutboxWorkerSnapshot(
            worker_id=worker_id,
            state="starting",
            last_heartbeat_at=None,
            last_success_at=None,
            last_error_code=None,
            claimed_count=0,
            delivered_count=0,
            dead_count=0,
        )

    def request_stop(self) -> None:
        """Signal cooperative shutdown without doing I/O."""

        self._stop.set()

    def snapshot(self) -> OutboxWorkerSnapshot:
        """Return the latest immutable payload-free worker state."""

        return self._snapshot

    def run(self, *, once: bool = False) -> OutboxWorkerSnapshot:
        """Run one bounded batch or poll until cooperative shutdown."""

        previous_handlers = self._install_signal_handlers()
        try:
            self._publish(state="running")
            if once:
                if not self._stop.is_set():
                    self._run_one_batch()
                self._publish(state="stopped")
                return self.snapshot()
            while not self._stop.is_set():
                claimed = self._run_one_batch()
                if self._stop.is_set():
                    break
                if claimed == 0:
                    self._publish(state="idle")
                    self._wait_for_poll()
                    self._publish(state="running")
            self._publish(state="stopping")
            self._publish(state="stopped")
            return self.snapshot()
        finally:
            self._restore_signal_handlers(previous_handlers)

    def _wait_for_poll(self) -> None:
        remaining = self._settings.poll_interval_seconds
        while remaining > 0 and not self._stop.is_set():
            interval = min(
                remaining,
                self._settings.heartbeat_interval_seconds,
            )
            if self._sleeper is None:
                self._stop.wait(interval)
            else:
                self._sleeper(interval)
            remaining -= interval
            if not self._stop.is_set() and remaining > 0:
                self._publish(state="idle")

    def _run_one_batch(self) -> int:
        now = self._now()
        try:
            records = self._repository.claim_outbox_batch(
                self._snapshot.worker_id,
                now=now,
                lease_until=now + timedelta(seconds=self._settings.lease_seconds),
                batch_size=self._settings.batch_size,
            )
        except Exception as error:
            self._record_error("OUTBOX_DATABASE_UNAVAILABLE", error)
            return 0
        self._snapshot = replace(
            self._snapshot,
            claimed_count=self._snapshot.claimed_count + len(records),
        )
        self._publish(state="running")
        for index, record in enumerate(records):
            if self._stop.is_set():
                break
            remaining = records[index:]
            if not self._renew(remaining):
                continue
            self._deliver_one(record, leased_records=remaining)
        return len(records)

    def _renew(self, records: tuple[OutboxRecord, ...]) -> bool:
        now = self._now()
        references = [(record.event_id, record.version) for record in records]
        try:
            renewed = self._repository.renew_outbox_leases(
                self._snapshot.worker_id,
                references,
                now=now,
                lease_until=now + timedelta(seconds=self._settings.lease_seconds),
            )
        except Exception as error:
            self._record_error("OUTBOX_DATABASE_UNAVAILABLE", error)
            return False
        if not renewed or renewed[0] != records[0].event_id:
            self._record_error("OUTBOX_LEASE_STALE")
            return False
        self._publish(state="running")
        return True

    def _deliver_one(
        self,
        record: OutboxRecord,
        *,
        leased_records: tuple[OutboxRecord, ...],
    ) -> None:
        try:
            lease_current = self._append_with_heartbeat(
                record,
                leased_records=leased_records,
            )
        except Exception as error:
            self._handle_delivery_failure(record, error)
            return
        if not lease_current:
            return
        try:
            acknowledged = self._repository.mark_outbox_delivered(
                self._snapshot.worker_id,
                [(record.event_id, record.version)],
            )
        except Exception as error:
            self._record_error("OUTBOX_DATABASE_UNAVAILABLE", error)
            return
        if acknowledged != (record.event_id,):
            self._record_error("OUTBOX_ACK_STALE")
            return
        succeeded_at = self._now()
        self._snapshot = replace(
            self._snapshot,
            last_success_at=succeeded_at,
            delivered_count=self._snapshot.delivered_count + 1,
        )
        self._publish(state="running", now=succeeded_at)
        self._log("outbox.record_delivered", delivered_count=1)

    def _append_with_heartbeat(
        self,
        record: OutboxRecord,
        *,
        leased_records: tuple[OutboxRecord, ...],
    ) -> bool:
        completed = threading.Event()
        errors: list[BaseException] = []

        def append() -> None:
            try:
                self._sink.append_if_absent(
                    record.event_id,
                    record.serialized_record,
                )
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()

        sink_thread = threading.Thread(
            target=append,
            name="course-insight-outbox-sink",
            daemon=False,
        )
        sink_thread.start()
        lease_current = True
        heartbeat_interval = self._settings.heartbeat_interval_seconds
        next_heartbeat = time.monotonic() + heartbeat_interval
        while not completed.is_set():
            remaining = max(0.0, next_heartbeat - time.monotonic())
            if completed.wait(min(remaining, 0.1)):
                break
            if time.monotonic() >= next_heartbeat:
                if lease_current:
                    lease_current = self._renew(leased_records)
                next_heartbeat = time.monotonic() + heartbeat_interval
        if errors:
            error = errors[0]
            if isinstance(error, Exception):
                raise error
            raise RuntimeError("outbox sink terminated abnormally")
        return lease_current

    def _handle_delivery_failure(
        self,
        record: OutboxRecord,
        error: Exception,
    ) -> None:
        error_code = _delivery_error_code(error)
        now = self._now()
        dead = record.attempt_count >= self._settings.max_retries
        next_attempt_at = now + timedelta(
            seconds=0 if dead else self._retry_delay(record.attempt_count)
        )
        try:
            changed = self._repository.mark_outbox_failed(
                self._snapshot.worker_id,
                record.event_id,
                expected_version=record.version,
                now=now,
                next_attempt_at=next_attempt_at,
                error_code=error_code,
                dead=dead,
            )
        except Exception as database_error:
            self._record_error("OUTBOX_DATABASE_UNAVAILABLE", database_error)
            return
        if not changed:
            self._record_error("OUTBOX_FAILURE_STALE")
            return
        self._snapshot = replace(
            self._snapshot,
            last_error_code=error_code,
            dead_count=self._snapshot.dead_count + (1 if dead else 0),
        )
        self._publish(state="running", now=now)
        self._log(
            "outbox.record_failed",
            error=error,
            error_code=error_code,
            status="dead" if dead else "pending",
            dead_count=1 if dead else 0,
        )

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = min(max(attempt_count - 1, 0), 62)
        base_delay = min(
            self._settings.retry_max_seconds,
            self._settings.retry_base_seconds * (2**exponent),
        )
        sample = self._jitter()
        if type(sample) not in {int, float} or not math.isfinite(float(sample)):
            raise ValueError("jitter must return a finite number")
        bounded_sample = min(1.0, max(0.0, float(sample)))
        multiplier = 1 + self._settings.retry_jitter_ratio * (
            (2 * bounded_sample) - 1
        )
        return max(0.0, min(self._settings.retry_max_seconds, base_delay * multiplier))

    def _record_error(
        self,
        error_code: str,
        error: Exception | None = None,
    ) -> None:
        self._snapshot = replace(
            self._snapshot,
            last_error_code=error_code,
        )
        self._publish(state="running")
        self._log(
            "outbox.worker_error",
            error=error,
            error_code=error_code,
        )

    def _publish(
        self,
        *,
        state: WorkerState,
        now: datetime | None = None,
    ) -> None:
        heartbeat = self._now() if now is None else now
        self._snapshot = replace(
            self._snapshot,
            state=state,
            last_heartbeat_at=heartbeat,
        )
        write_json(self._status_path, self._snapshot.to_status_dict())

    def _now(self) -> datetime:
        return validate_utc_datetime(self._clock(), field="clock result")

    def _log(
        self,
        event: str,
        *,
        error: Exception | None = None,
        error_code: str | None = None,
        **fields: object,
    ) -> None:
        if self._logger is not None:
            with bind_log_context(worker_id=self._snapshot.worker_id):
                log_event(
                    self._logger,
                    event,
                    error=error,
                    error_code=error_code,
                    **fields,
                )

    def _install_signal_handlers(
        self,
    ) -> dict[signal.Signals, object]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        previous: dict[signal.Signals, object] = {}

        def stop_handler(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            self.request_stop()

        for candidate in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[candidate] = signal.getsignal(candidate)
                signal.signal(candidate, stop_handler)
            except (OSError, RuntimeError, ValueError):
                previous.pop(candidate, None)
        return previous

    @staticmethod
    def _restore_signal_handlers(
        previous: dict[signal.Signals, object],
    ) -> None:
        for candidate, handler in previous.items():
            try:
                signal.signal(candidate, handler)  # type: ignore[arg-type]
            except (OSError, RuntimeError, ValueError):
                continue


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _delivery_error_code(error: Exception) -> str:
    if isinstance(error, (ValueError, UnicodeError)):
        return "OUTBOX_RECORD_INVALID"
    if isinstance(error, OSError):
        return "OUTBOX_SINK_UNAVAILABLE"
    return "OUTBOX_SINK_FAILED"


__all__ = ["OutboxSink", "OutboxWorker", "OutboxWorkerSnapshot", "WorkerState"]
