"""Private immutable outbox records and the durable idempotent audit sink."""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterator, Literal

from course_insight.infrastructure.json_io import dumps_json


OutboxStatus = Literal["pending", "processing", "dead"]
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def validate_outbox_identifier(value: str, *, field: str) -> str:
    if type(value) is not str or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a safe identifier")
    return value


def validate_event_id(value: str) -> str:
    """Preserve the public LearningEvent contract: any non-empty UTF-8 string."""

    if type(value) is not str or not value:
        raise ValueError("event_id must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("event_id must be valid UTF-8") from None
    return value


def validate_error_code(value: str | None) -> str | None:
    if value is not None and (
        type(value) is not str or not _SAFE_ERROR_CODE.fullmatch(value)
    ):
        raise ValueError("last_error_code must be a safe error code")
    return value


def validate_utc_datetime(value: datetime, *, field: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field} must be an aware UTC datetime")
    return value


def utc_text(value: datetime) -> str:
    validated = validate_utc_datetime(value, field="timestamp")
    return validated.isoformat(timespec="microseconds")


def parse_utc_text(value: str, *, field: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a timestamp") from None
    return validate_utc_datetime(parsed, field=field)


def validate_serialized_record(event_id: str, serialized_record: str) -> str:
    validate_event_id(event_id)
    if type(serialized_record) is not str:
        raise ValueError("serialized_record must be canonical JSON")
    try:
        value = json.loads(
            serialized_record,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("serialized_record must be canonical JSON") from None
    if type(value) is not dict or value.get("event_id") != event_id:
        raise ValueError("serialized_record event_id does not match")
    if dumps_json(value) != serialized_record:
        raise ValueError("serialized_record must be canonical JSON")
    return serialized_record


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """One private, payload-bearing delivery row isolated from public contracts."""

    event_id: str
    serialized_record: str
    status: OutboxStatus
    attempt_count: int
    version: int
    available_at: datetime
    locked_by: str | None
    locked_at: datetime | None
    lease_until: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        validate_serialized_record(self.event_id, self.serialized_record)
        if self.status not in {"pending", "processing", "dead"}:
            raise ValueError("status is invalid")
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("version must be positive")
        validate_utc_datetime(self.available_at, field="available_at")
        validate_utc_datetime(self.created_at, field="created_at")
        validate_utc_datetime(self.updated_at, field="updated_at")
        validate_error_code(self.last_error_code)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        lock_values = (self.locked_by, self.locked_at, self.lease_until)
        if self.status == "processing":
            if any(value is None for value in lock_values):
                raise ValueError("processing records require one complete lease")
            locked_by = self.locked_by
            locked_at = self.locked_at
            lease_until = self.lease_until
            if locked_by is None or locked_at is None or lease_until is None:
                raise ValueError("processing records require one complete lease")
            validate_outbox_identifier(locked_by, field="locked_by")
            validate_utc_datetime(locked_at, field="locked_at")
            validate_utc_datetime(lease_until, field="lease_until")
            if lease_until <= locked_at:
                raise ValueError("lease_until must follow locked_at")
        elif any(value is not None for value in lock_values):
            raise ValueError("unclaimed records cannot retain lease fields")


class IdempotentJsonlSink:
    """Append canonical domain events once, serialized across processes."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(f".{self._path.name}.lock")

    def append_if_absent(
        self,
        event_id: str,
        serialized_record: str,
    ) -> bool:
        """Durably append once by event identity; reject any corrupt history."""

        validate_serialized_record(event_id, serialized_record)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.is_symlink() or self._lock_path.is_symlink():
            raise ValueError("audit sink cannot use symbolic links")
        with _exclusive_path_lock(self._lock_path):
            existing = self._validated_existing_records()
            previous = existing.get(event_id)
            if previous is not None:
                if previous != serialized_record:
                    raise ValueError("event_id already has a different record")
                return False
            with self._path.open(
                mode="a",
                encoding="utf-8",
                newline="",
            ) as output:
                output.write(f"{serialized_record}\n")
                output.flush()
                os.fsync(output.fileno())
        return True

    def _validated_existing_records(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            content = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ValueError("audit log is unreadable") from None
        if content and not content.endswith("\n"):
            raise ValueError("audit log has a partial row")
        records: dict[str, str] = {}
        for line in content.splitlines():
            if not line:
                raise ValueError("audit log contains an empty row")
            try:
                candidate = json.loads(line, parse_constant=_reject_json_constant)
            except (ValueError, json.JSONDecodeError):
                raise ValueError("audit log contains invalid JSON") from None
            if type(candidate) is not dict:
                raise ValueError("audit log row must be an object")
            candidate_id = candidate.get("event_id")
            validate_event_id(candidate_id)
            validate_serialized_record(candidate_id, line)
            if candidate_id in records:
                raise ValueError("audit log contains a duplicate event_id")
            records[candidate_id] = line
        return records


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_path_lock(path: Path) -> Iterator[None]:
    local_lock = _path_lock(path)
    with local_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock_file:
            _ensure_lock_byte(lock_file)
            _lock_file(lock_file)
            try:
                yield
            finally:
                _unlock_file(lock_file)


def _ensure_lock_byte(lock_file: IO[bytes]) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
        os.fsync(lock_file.fileno())
    lock_file.seek(0)


def _lock_file(lock_file: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file: IO[bytes]) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


__all__ = ["IdempotentJsonlSink", "OutboxRecord", "OutboxStatus"]
