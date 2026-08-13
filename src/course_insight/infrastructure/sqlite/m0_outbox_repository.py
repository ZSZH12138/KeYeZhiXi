"""Short-transaction SQLite outbox operations shared by M0 repositories."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.modules.m0_platform.lease_heartbeat import (
    run_with_lease_heartbeat,
)
from course_insight.modules.m0_platform.outbox import (
    OutboxRecord,
    parse_utc_text,
    utc_text,
    validate_error_code,
    validate_event_id,
    validate_outbox_identifier,
    validate_utc_datetime,
)


class SQLiteM0OutboxRepositoryMixin:
    """Lease/CAS operations; every method owns one bounded transaction."""

    _compat_lease_seconds = 30.0
    _compat_heartbeat_interval_seconds = 10.0
    _database_path: Path
    _outbox_clock: Callable[[], datetime]

    def append_events(
        self,
        events: Sequence[LearningEvent],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Insert learning events and pending delivery rows atomically."""

        accepted_event_ids: list[str] = []
        duplicate_event_ids: list[str] = []
        seen_input: set[str] = set()
        enqueue_at = utc_text(
            validate_utc_datetime(
                self._outbox_clock(),
                field="outbox clock result",
            )
        )
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for event in events:
                idempotency_key = event.idempotency_key()
                if idempotency_key in seen_input:
                    continue
                seen_input.add(idempotency_key)
                exists = connection.execute(
                    """
                    SELECT 1 FROM m0_learning_events
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if exists is not None:
                    duplicate_event_ids.append(event.event_id)
                    continue
                occurred_at = utc_text(
                    event.occurred_at.astimezone(timezone.utc)
                )
                connection.execute(
                    """
                    INSERT INTO m0_learning_events(
                        event_id,
                        idempotency_key,
                        event_type,
                        occurred_at,
                        payload
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        idempotency_key,
                        event.event_type,
                        occurred_at,
                        dumps_json(event.payload),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO m0_event_outbox(
                        event_id,
                        record,
                        status,
                        attempt_count,
                        version,
                        available_at,
                        locked_by,
                        locked_at,
                        lease_until,
                        last_error_code,
                        created_at,
                        updated_at
                    ) VALUES (
                        ?, ?, 'pending', 0, 1, ?, NULL, NULL, NULL, NULL, ?, ?
                    )
                    """,
                    (
                        event.event_id,
                        dumps_json(event.to_dict()),
                        enqueue_at,
                        enqueue_at,
                        enqueue_at,
                    ),
                )
                accepted_event_ids.append(event.event_id)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return tuple(accepted_event_ids), tuple(duplicate_event_ids)

    def claim_outbox_batch(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> tuple[OutboxRecord, ...]:
        validate_outbox_identifier(worker_id, field="worker_id")
        validate_utc_datetime(now, field="now")
        validate_utc_datetime(lease_until, field="lease_until")
        if lease_until <= now:
            raise ValueError("lease_until must follow now")
        if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size is outside the supported range")
        now_text = utc_text(now)
        lease_text = utc_text(lease_until)
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidates = connection.execute(
                """
                SELECT event_id, version
                FROM m0_event_outbox
                WHERE (
                    status = 'pending'
                    AND julianday(available_at) <= julianday(?)
                ) OR (
                    status = 'processing'
                    AND julianday(lease_until) <= julianday(?)
                )
                ORDER BY
                    julianday(available_at),
                    julianday(created_at),
                    event_id
                LIMIT ?
                """,
                (now_text, now_text, batch_size),
            ).fetchall()
            claimed_ids: list[str] = []
            for candidate in candidates:
                cursor = connection.execute(
                    """
                    UPDATE m0_event_outbox
                    SET status = 'processing',
                        attempt_count = attempt_count + 1,
                        version = version + 1,
                        locked_by = ?,
                        locked_at = ?,
                        lease_until = ?,
                        updated_at = ?
                    WHERE event_id = ? AND version = ? AND (
                        (
                            status = 'pending'
                            AND julianday(available_at) <= julianday(?)
                        ) OR (
                            status = 'processing'
                            AND julianday(lease_until) <= julianday(?)
                        )
                    )
                    """,
                    (
                        worker_id,
                        now_text,
                        lease_text,
                        now_text,
                        str(candidate["event_id"]),
                        int(candidate["version"]),
                        now_text,
                        now_text,
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_ids.append(str(candidate["event_id"]))
            records = tuple(
                _outbox_record(row)
                for event_id in claimed_ids
                for row in [
                    connection.execute(
                        """
                        SELECT * FROM m0_event_outbox WHERE event_id = ?
                        """,
                        (event_id,),
                    ).fetchone()
                ]
                if row is not None
            )
            connection.execute("COMMIT")
            return records
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_outbox_delivered(
        self,
        worker_id: str,
        records: Sequence[tuple[str, int]],
    ) -> tuple[str, ...]:
        validate_outbox_identifier(worker_id, field="worker_id")
        references = _validate_references(records)
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            delivered: list[str] = []
            for event_id, version in references:
                cursor = connection.execute(
                    """
                    DELETE FROM m0_event_outbox
                    WHERE event_id = ?
                      AND status = 'processing'
                      AND locked_by = ?
                      AND version = ?
                    """,
                    (event_id, worker_id, version),
                )
                if cursor.rowcount == 1:
                    delivered.append(event_id)
            connection.execute("COMMIT")
            return tuple(delivered)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_outbox_failed(
        self,
        worker_id: str,
        event_id: str,
        *,
        expected_version: int,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
        dead: bool,
    ) -> bool:
        validate_outbox_identifier(worker_id, field="worker_id")
        validate_event_id(event_id)
        _validate_version(expected_version)
        validate_utc_datetime(now, field="now")
        validate_utc_datetime(next_attempt_at, field="next_attempt_at")
        validate_error_code(error_code)
        if next_attempt_at < now:
            raise ValueError("next_attempt_at precedes now")
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE m0_event_outbox
                SET status = ?,
                    version = version + 1,
                    available_at = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    lease_until = NULL,
                    last_error_code = ?,
                    updated_at = ?
                WHERE event_id = ?
                  AND status = 'processing'
                  AND locked_by = ?
                  AND version = ?
                """,
                (
                    "dead" if dead else "pending",
                    utc_text(next_attempt_at),
                    error_code,
                    utc_text(now),
                    event_id,
                    worker_id,
                    expected_version,
                ),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def renew_outbox_leases(
        self,
        worker_id: str,
        records: Sequence[tuple[str, int]],
        *,
        now: datetime,
        lease_until: datetime,
    ) -> tuple[str, ...]:
        validate_outbox_identifier(worker_id, field="worker_id")
        references = _validate_references(records)
        validate_utc_datetime(now, field="now")
        validate_utc_datetime(lease_until, field="lease_until")
        if lease_until <= now:
            raise ValueError("lease_until must follow now")
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            renewed: list[str] = []
            for event_id, version in references:
                cursor = connection.execute(
                    """
                    UPDATE m0_event_outbox
                    SET lease_until = ?, updated_at = ?
                    WHERE event_id = ?
                      AND status = 'processing'
                      AND locked_by = ?
                      AND version = ?
                      AND julianday(lease_until) > julianday(?)
                    """,
                    (
                        utc_text(lease_until),
                        utc_text(now),
                        event_id,
                        worker_id,
                        version,
                        utc_text(now),
                    ),
                )
                if cursor.rowcount == 1:
                    renewed.append(event_id)
            connection.execute("COMMIT")
            return tuple(renewed)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def deliver_outbox_records(
        self,
        deliver: Callable[[str, str], None],
    ) -> None:
        """Deliver callbacks outside transactions while retaining the lease."""

        worker_id = f"compat-{os.getpid()}-{threading.get_ident()}"
        while True:
            now = validate_utc_datetime(
                self._outbox_clock(),
                field="outbox clock result",
            )
            records = self.claim_outbox_batch(
                worker_id,
                now=now,
                lease_until=now
                + timedelta(seconds=self._compat_lease_seconds),
                batch_size=1,
            )
            if not records:
                return
            for record in records:
                outcome = run_with_lease_heartbeat(
                    lambda: deliver(
                        record.event_id,
                        record.serialized_record,
                    ),
                    renew=lambda: self._renew_compat_record(
                        worker_id,
                        record,
                    ),
                    heartbeat_interval_seconds=(
                        self._compat_heartbeat_interval_seconds
                    ),
                )
                if outcome.error is not None:
                    if outcome.lease_current and isinstance(
                        outcome.error,
                        Exception,
                    ):
                        failed_at = validate_utc_datetime(
                            self._outbox_clock(),
                            field="outbox clock result",
                        )
                        self.mark_outbox_failed(
                            worker_id,
                            record.event_id,
                            expected_version=record.version,
                            now=failed_at,
                            next_attempt_at=failed_at,
                            error_code="OUTBOX_DELIVERY_FAILED",
                            dead=False,
                        )
                    raise outcome.error
                if not outcome.lease_current:
                    raise RuntimeError("outbox delivery lease was lost")
                acknowledged = self.mark_outbox_delivered(
                    worker_id,
                    [(record.event_id, record.version)],
                )
                if acknowledged != (record.event_id,):
                    raise RuntimeError("outbox delivery acknowledgement was stale")

    def _renew_compat_record(
        self,
        worker_id: str,
        record: OutboxRecord,
    ) -> bool:
        renewed_at = validate_utc_datetime(
            self._outbox_clock(),
            field="outbox clock result",
        )
        renewed = self.renew_outbox_leases(
            worker_id,
            [(record.event_id, record.version)],
            now=renewed_at,
            lease_until=renewed_at
            + timedelta(seconds=self._compat_lease_seconds),
        )
        return renewed == (record.event_id,)


def _outbox_record(row: sqlite3.Row) -> OutboxRecord:
    return OutboxRecord(
        event_id=str(row["event_id"]),
        serialized_record=str(row["record"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        attempt_count=int(row["attempt_count"]),
        version=int(row["version"]),
        available_at=parse_utc_text(
            str(row["available_at"]),
            field="available_at",
        ),
        locked_by=(
            None
            if row["locked_by"] is None
            else str(row["locked_by"])
        ),
        locked_at=_optional_time(row["locked_at"], field="locked_at"),
        lease_until=_optional_time(row["lease_until"], field="lease_until"),
        last_error_code=(
            None
            if row["last_error_code"] is None
            else str(row["last_error_code"])
        ),
        created_at=parse_utc_text(
            str(row["created_at"]),
            field="created_at",
        ),
        updated_at=parse_utc_text(
            str(row["updated_at"]),
            field="updated_at",
        ),
    )


def _optional_time(value: object, *, field: str) -> datetime | None:
    return None if value is None else parse_utc_text(str(value), field=field)


def _validate_references(
    records: Sequence[tuple[str, int]],
) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    seen: set[str] = set()
    for event_id, version in records:
        validate_event_id(event_id)
        _validate_version(version)
        if event_id in seen:
            raise ValueError("duplicate outbox reference")
        seen.add(event_id)
        result.append((event_id, version))
    return tuple(result)


def _validate_version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("version must be positive")
    return value


__all__ = ["SQLiteM0OutboxRepositoryMixin"]
