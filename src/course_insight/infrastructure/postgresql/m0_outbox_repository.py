"""PostgreSQL event/outbox transactions for the M0 repository."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import psycopg

from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.modules.m0_platform.lease_heartbeat import (
    run_with_lease_heartbeat,
)
from course_insight.modules.m0_platform.outbox import (
    OutboxRecord,
    validate_error_code,
    validate_event_id,
    validate_outbox_identifier,
    validate_utc_datetime,
)


class _Transaction(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> bool | None: ...


class _Cursor(Protocol):
    rowcount: int

    def fetchone(self) -> Mapping[str, object] | None: ...

    def fetchall(self) -> Sequence[Mapping[str, object]]: ...


class _Connection(Protocol):
    def transaction(self) -> _Transaction: ...

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor: ...


class _Pool(Protocol):
    def connection(self) -> Any: ...


class PostgresM0RepositoryError(PostgresOperationError):
    """Stable M0 persistence failure that contains no driver or DSN details."""


@contextmanager
def m0_transaction(pool: _Pool) -> Iterator[_Connection]:
    """Open one bounded transaction and translate all driver exceptions."""

    try:
        with pool.connection() as connection:
            with connection.transaction():
                yield connection
    except (DomainError, ValueError, PostgresError):
        raise
    except psycopg.Error:
        raise PostgresM0RepositoryError(
            "PostgreSQL M0 repository operation failed"
        ) from None


class PostgresM0OutboxRepositoryMixin:
    """Atomic event/outbox persistence and short lease/CAS operations."""

    _compat_lease_seconds = 30.0
    _compat_heartbeat_interval_seconds = 10.0
    _pool: _Pool
    _outbox_clock: Callable[[], datetime]

    def append_events(
        self,
        events: Sequence[LearningEvent],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Insert each new event and its canonical outbox row atomically."""

        enqueue_at = validate_utc_datetime(
            self._outbox_clock(),
            field="outbox clock result",
        )
        prepared = _prepare_events(events)
        accepted: list[str] = []
        duplicates: list[str] = []
        with m0_transaction(self._pool) as connection:
            for event, payload, record, occurred_at in prepared:
                inserted = connection.execute(
                    """
                    INSERT INTO m0_learning_events(
                        event_id,
                        idempotency_key,
                        event_type,
                        occurred_at,
                        payload
                    ) VALUES (%s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event.event_id,
                        event.idempotency_key(),
                        event.event_type,
                        occurred_at,
                        payload,
                    ),
                ).fetchone()
                if inserted is None:
                    duplicates.append(event.event_id)
                    continue
                if _returned_event_id(inserted) != event.event_id:
                    raise ValueError("inserted event identity is inconsistent")
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
                        %s, %s, 'pending', 0, 1, %s,
                        NULL, NULL, NULL, NULL, %s, %s
                    )
                    """,
                    (
                        event.event_id,
                        record,
                        enqueue_at,
                        enqueue_at,
                        enqueue_at,
                    ),
                )
                accepted.append(event.event_id)
        return tuple(accepted), tuple(duplicates)

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

        with m0_transaction(self._pool) as connection:
            candidates = connection.execute(
                """
                SELECT event_id, version
                FROM m0_event_outbox
                WHERE (
                    status = 'pending'
                    AND available_at <= %s
                ) OR (
                    status = 'processing'
                    AND lease_until <= %s
                )
                ORDER BY available_at, created_at, event_id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (now, now, batch_size),
            ).fetchall()
            claimed: list[OutboxRecord] = []
            for candidate in candidates:
                event_id, version = _outbox_reference_row(candidate)
                row = connection.execute(
                    f"""
                    UPDATE m0_event_outbox
                    SET status = 'processing',
                        attempt_count = attempt_count + 1,
                        version = version + 1,
                        locked_by = %s,
                        locked_at = %s,
                        lease_until = %s,
                        updated_at = %s
                    WHERE event_id = %s
                      AND version = %s
                      AND (
                          (
                              status = 'pending'
                              AND available_at <= %s
                          ) OR (
                              status = 'processing'
                              AND lease_until <= %s
                          )
                      )
                    RETURNING {_OUTBOX_COLUMNS}
                    """,
                    (
                        worker_id,
                        now,
                        lease_until,
                        now,
                        event_id,
                        version,
                        now,
                        now,
                    ),
                ).fetchone()
                if row is not None:
                    claimed.append(_outbox_record(row))
            return tuple(claimed)

    def mark_outbox_delivered(
        self,
        worker_id: str,
        records: Sequence[tuple[str, int]],
    ) -> tuple[str, ...]:
        validate_outbox_identifier(worker_id, field="worker_id")
        references = _validate_references(records)
        with m0_transaction(self._pool) as connection:
            delivered: list[str] = []
            for event_id, version in references:
                row = connection.execute(
                    """
                    DELETE FROM m0_event_outbox
                    WHERE event_id = %s
                      AND status = 'processing'
                      AND locked_by = %s
                      AND version = %s
                    RETURNING event_id
                    """,
                    (event_id, worker_id, version),
                ).fetchone()
                if row is not None:
                    delivered.append(_returned_event_id(row))
            return tuple(delivered)

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
        with m0_transaction(self._pool) as connection:
            row = connection.execute(
                """
                UPDATE m0_event_outbox
                SET status = %s,
                    version = version + 1,
                    available_at = %s,
                    locked_by = NULL,
                    locked_at = NULL,
                    lease_until = NULL,
                    last_error_code = %s,
                    updated_at = %s
                WHERE event_id = %s
                  AND status = 'processing'
                  AND locked_by = %s
                  AND version = %s
                RETURNING event_id
                """,
                (
                    "dead" if dead else "pending",
                    next_attempt_at,
                    error_code,
                    now,
                    event_id,
                    worker_id,
                    expected_version,
                ),
            ).fetchone()
            return row is not None

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
        with m0_transaction(self._pool) as connection:
            renewed: list[str] = []
            for event_id, version in references:
                row = connection.execute(
                    """
                    UPDATE m0_event_outbox
                    SET lease_until = %s, updated_at = %s
                    WHERE event_id = %s
                      AND status = 'processing'
                      AND lease_until > %s
                      AND locked_by = %s
                      AND version = %s
                    RETURNING event_id
                    """,
                    (
                        lease_until,
                        now,
                        event_id,
                        now,
                        worker_id,
                        version,
                    ),
                ).fetchone()
                if row is not None:
                    renewed.append(_returned_event_id(row))
            return tuple(renewed)

    def deliver_outbox_records(
        self,
        deliver: Callable[[str, str], None],
    ) -> None:
        """Run committed callbacks while retaining compatibility-path leases."""

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
                    raise RuntimeError(
                        "outbox delivery acknowledgement was stale"
                    )

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


_OUTBOX_COLUMNS = """
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
"""


def _prepare_events(
    events: Sequence[LearningEvent],
) -> tuple[tuple[LearningEvent, str, str, datetime], ...]:
    prepared: list[tuple[LearningEvent, str, str, datetime]] = []
    seen: set[str] = set()
    for event in events:
        event_id = validate_event_id(event.event_id)
        if event_id in seen:
            continue
        seen.add(event_id)
        occurred_at = event.occurred_at.astimezone(timezone.utc)
        validate_utc_datetime(occurred_at, field="occurred_at")
        prepared.append(
            (
                event,
                dumps_json(event.payload),
                dumps_json(event.to_dict()),
                occurred_at,
            )
        )
    return tuple(prepared)


def _outbox_reference_row(row: Mapping[str, object]) -> tuple[str, int]:
    try:
        event_id = row["event_id"]
        version = row["version"]
    except (KeyError, TypeError):
        raise ValueError("outbox row is invalid") from None
    validate_event_id(event_id)
    _validate_version(version)
    return event_id, version


def _outbox_record(row: Mapping[str, object]) -> OutboxRecord:
    try:
        return OutboxRecord(
            event_id=row["event_id"],  # type: ignore[arg-type]
            serialized_record=row["record"],  # type: ignore[arg-type]
            status=row["status"],  # type: ignore[arg-type]
            attempt_count=row["attempt_count"],  # type: ignore[arg-type]
            version=row["version"],  # type: ignore[arg-type]
            available_at=_as_utc(row["available_at"], field="available_at"),
            locked_by=row["locked_by"],  # type: ignore[arg-type]
            locked_at=_optional_utc(row["locked_at"], field="locked_at"),
            lease_until=_optional_utc(
                row["lease_until"],
                field="lease_until",
            ),
            last_error_code=row["last_error_code"],  # type: ignore[arg-type]
            created_at=_as_utc(row["created_at"], field="created_at"),
            updated_at=_as_utc(row["updated_at"], field="updated_at"),
        )
    except (KeyError, TypeError):
        raise ValueError("outbox row is invalid") from None


def _as_utc(value: object, *, field: str) -> datetime:
    if type(value) is datetime:
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field} must be a timestamp") from None
    else:
        raise ValueError(f"{field} must be a timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an aware timestamp")
    return parsed.astimezone(timezone.utc)


def _optional_utc(value: object, *, field: str) -> datetime | None:
    return None if value is None else _as_utc(value, field=field)


def _returned_event_id(row: Mapping[str, object]) -> str:
    try:
        event_id = row["event_id"]
    except (KeyError, TypeError):
        raise ValueError("outbox result row is invalid") from None
    return validate_event_id(event_id)


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


__all__ = [
    "PostgresM0OutboxRepositoryMixin",
    "PostgresM0RepositoryError",
    "m0_transaction",
]
