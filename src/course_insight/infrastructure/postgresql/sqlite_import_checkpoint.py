"""Durable, privacy-safe checkpoints for SQLite-to-PostgreSQL imports."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from course_insight.infrastructure.json_io import (
    dumps_json,
    read_json,
    write_json,
)


CheckpointStatus = Literal["applying", "completed"]
_CHECKPOINT_VERSION = 1
_SHA256_LENGTH = 64


class CheckpointError(RuntimeError):
    """Stable checkpoint failure without source, target, or row details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CheckpointBinding:
    """Immutable import inputs that make a cursor safe to reuse."""

    source_snapshot_checksum: str
    source_schema_version: int
    migration_version: int
    destination_fingerprint: str
    batch_size: int
    total_batches: int
    partial_envelope_rows: int
    table_order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _is_sha256(self.source_snapshot_checksum):
            raise ValueError("source snapshot checksum must be SHA-256")
        validate_destination_fingerprint(self.destination_fingerprint)
        integer_fields = (
            self.source_schema_version,
            self.migration_version,
            self.batch_size,
            self.total_batches,
            self.partial_envelope_rows,
        )
        if any(type(value) is not int or value < 0 for value in integer_fields):
            raise ValueError("checkpoint binding integers must be non-negative")
        if self.source_schema_version < 1 or self.migration_version < 1:
            raise ValueError("checkpoint schema versions must be positive")
        if not 1 <= self.batch_size <= 10_000:
            raise ValueError("checkpoint batch_size is outside its limit")
        if (
            not self.table_order
            or any(type(table) is not str or not table for table in self.table_order)
        ):
            raise ValueError("checkpoint table order must be non-empty")

    @property
    def import_plan_checksum(self) -> str:
        return _digest(
            {
                "source_schema_version": self.source_schema_version,
                "migration_version": self.migration_version,
                "batch_size": self.batch_size,
                "table_order": list(self.table_order),
            }
        )


@dataclass(frozen=True, slots=True)
class CheckpointState:
    status: CheckpointStatus
    next_batch_index: int


def validate_destination_fingerprint(value: str | None) -> str | None:
    """Accept only an opaque lower-case SHA-256 target binding."""

    if value is None:
        return None
    if not _is_sha256(value):
        raise ValueError("destination_fingerprint must be a SHA-256 digest")
    return value


def validated_checkpoint_path(
    source: Path,
    checkpoint_path: str | os.PathLike[str] | None,
    *,
    report_path: Path | None,
) -> Path | None:
    if checkpoint_path is None:
        return None
    target = Path(checkpoint_path).resolve()
    if target == source:
        raise ValueError(
            "checkpoint_path must not overwrite the SQLite source"
        )
    if report_path is not None and target == report_path:
        raise ValueError("checkpoint_path must differ from report_path")
    return target


def load_or_create_checkpoint(
    path: Path,
    binding: CheckpointBinding,
) -> CheckpointState:
    """Load a strictly bound cursor or atomically create its zero state."""

    if not path.exists():
        state = CheckpointState(status="applying", next_batch_index=0)
        write_checkpoint(path, binding, state)
        return state
    try:
        value = read_json(path)
        return _validated_checkpoint(value, binding)
    except CheckpointError:
        raise
    except Exception:
        raise CheckpointError("MIGRATION_CHECKPOINT_INVALID") from None


def write_checkpoint(
    path: Path,
    binding: CheckpointBinding,
    state: CheckpointState,
) -> None:
    """Atomically persist the next batch cursor after a committed batch."""

    if (
        type(state.next_batch_index) is not int
        or not 0 <= state.next_batch_index <= binding.total_batches
        or (
            state.status == "completed"
            and state.next_batch_index != binding.total_batches
        )
    ):
        raise ValueError("checkpoint state is outside the import plan")
    unsigned: dict[str, Any] = {
        "checkpoint_version": _CHECKPOINT_VERSION,
        "status": state.status,
        "source_snapshot_checksum": binding.source_snapshot_checksum,
        "source_schema_version": binding.source_schema_version,
        "migration_version": binding.migration_version,
        "destination_fingerprint": binding.destination_fingerprint,
        "import_plan_checksum": binding.import_plan_checksum,
        "batch_size": binding.batch_size,
        "next_batch_index": state.next_batch_index,
        "total_batches": binding.total_batches,
        "partial_envelope_rows": binding.partial_envelope_rows,
    }
    checkpoint = {**unsigned, "checkpoint_checksum": _digest(unsigned)}
    try:
        write_json(path, checkpoint)
    except Exception:
        raise CheckpointError(
            "MIGRATION_CHECKPOINT_WRITE_FAILED"
        ) from None


def _validated_checkpoint(
    value: Any,
    binding: CheckpointBinding,
) -> CheckpointState:
    keys = {
        "checkpoint_version",
        "status",
        "source_snapshot_checksum",
        "source_schema_version",
        "migration_version",
        "destination_fingerprint",
        "import_plan_checksum",
        "batch_size",
        "next_batch_index",
        "total_batches",
        "partial_envelope_rows",
        "checkpoint_checksum",
    }
    if type(value) is not dict or set(value) != keys:
        raise CheckpointError("MIGRATION_CHECKPOINT_INVALID")
    checksum = value["checkpoint_checksum"]
    unsigned = {
        key: item
        for key, item in value.items()
        if key != "checkpoint_checksum"
    }
    status = value["status"]
    if (
        not _is_sha256(checksum)
        or checksum != _digest(unsigned)
        or type(value["checkpoint_version"]) is not int
        or type(status) is not str
        or status not in {"applying", "completed"}
        or type(value["source_schema_version"]) is not int
        or type(value["migration_version"]) is not int
        or type(value["batch_size"]) is not int
        or type(value["next_batch_index"]) is not int
        or type(value["total_batches"]) is not int
        or type(value["partial_envelope_rows"]) is not int
        or not _is_sha256(value["source_snapshot_checksum"])
        or not _is_sha256(value["destination_fingerprint"])
        or not _is_sha256(value["import_plan_checksum"])
    ):
        raise CheckpointError("MIGRATION_CHECKPOINT_INVALID")
    if value["checkpoint_version"] != _CHECKPOINT_VERSION:
        raise CheckpointError("MIGRATION_CHECKPOINT_VERSION_MISMATCH")
    if (
        value["source_schema_version"] != binding.source_schema_version
        or value["migration_version"] != binding.migration_version
    ):
        raise CheckpointError("MIGRATION_CHECKPOINT_VERSION_MISMATCH")
    if (
        value["source_snapshot_checksum"]
        != binding.source_snapshot_checksum
    ):
        raise CheckpointError("MIGRATION_CHECKPOINT_SOURCE_MISMATCH")
    if (
        value["destination_fingerprint"]
        != binding.destination_fingerprint
    ):
        raise CheckpointError(
            "MIGRATION_CHECKPOINT_DESTINATION_MISMATCH"
        )
    if (
        value["import_plan_checksum"] != binding.import_plan_checksum
        or value["batch_size"] != binding.batch_size
        or value["total_batches"] != binding.total_batches
        or value["partial_envelope_rows"]
        != binding.partial_envelope_rows
    ):
        raise CheckpointError("MIGRATION_CHECKPOINT_PLAN_MISMATCH")
    next_batch_index = value["next_batch_index"]
    if not 0 <= next_batch_index <= binding.total_batches:
        raise CheckpointError("MIGRATION_CHECKPOINT_INVALID")
    if status == "completed" and next_batch_index != binding.total_batches:
        raise CheckpointError("MIGRATION_CHECKPOINT_INVALID")
    return CheckpointState(
        status=status,
        next_batch_index=next_batch_index,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(dumps_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "CheckpointBinding",
    "CheckpointError",
    "CheckpointState",
    "load_or_create_checkpoint",
    "validate_destination_fingerprint",
    "validated_checkpoint_path",
    "write_checkpoint",
]
