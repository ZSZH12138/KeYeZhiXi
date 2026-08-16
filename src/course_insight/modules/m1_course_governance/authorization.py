"""One strict, byte-oriented authorization-manifest codec shared by M1."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import PurePosixPath, PureWindowsPath
from typing import Mapping

from course_insight.contracts.errors import DomainError


_AUTHORIZATION_FIELDS = (
    "file_name", "source_id", "expected_sha256", "authorized_by",
    "authorized_at", "license_note",
)
_LEGACY_AUTHORIZATION_FIELDS = tuple(
    field for field in _AUTHORIZATION_FIELDS if field != "expected_sha256"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
})


@dataclass(frozen=True, slots=True)
class _AuthorizationRow:
    file_name: str
    source_id: str
    expected_sha256: str
    authorized_by: str
    authorized_at: str
    license_note: str
    legacy_hash: bool


def _domain(message: str, *, file_name: str | None = None, reason: str | None = None) -> DomainError:
    details: dict[str, str] = {}
    if file_name is not None:
        details["file_name"] = file_name
    if reason is not None:
        details["reason"] = reason
    return DomainError(code="UNAUTHORIZED_SOURCE", module="m1", message=message, details=details)


def _is_safe_path_segment(value: str) -> bool:
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    reserved_stem = value.split(".", maxsplit=1)[0].rstrip(" .").upper()
    return (
        value == value.strip()
        and value not in {".", ".."}
        and not value.endswith(".")
        and value == windows_path.name == posix_path.name
        and not windows_path.is_absolute() and not posix_path.is_absolute()
        and not windows_path.drive and not posix_path.drive
        and not any(character in '\\/:?*<>|"' or ord(character) < 32 for character in value)
        and reserved_stem not in _WINDOWS_RESERVED_NAMES
    )


def parse_authorizations(payload: bytes) -> dict[str, _AuthorizationRow]:
    """Decode a byte-exact CSV snapshot and apply M1's manifest rules."""
    try:
        text = payload.decode("utf-8-sig")
        reader = csv.DictReader(StringIO(text, newline=""))
        headers = reader.fieldnames
        if (
            headers is None
            or len(headers) != len(set(headers))
            or frozenset(headers) not in {
                frozenset(_AUTHORIZATION_FIELDS),
                frozenset(_LEGACY_AUTHORIZATION_FIELDS),
            }
        ):
            raise _domain("authorization manifest has an invalid header")
        legacy_hash = frozenset(headers) == frozenset(_LEGACY_AUTHORIZATION_FIELDS)
        rows: dict[str, _AuthorizationRow] = {}
        source_ids: set[str] = set()
        for row in reader:
            if None in row:
                raise _domain("authorization manifest row has unexpected columns")
            value = {key: (item if isinstance(item, str) else "") for key, item in row.items() if key is not None}
            if legacy_hash:
                value["expected_sha256"] = ""
            _validate_row(value, legacy_hash=legacy_hash)
            file_name, source_id = value["file_name"], value["source_id"]
            if file_name in rows or source_id in source_ids:
                raise _domain("authorization manifest contains duplicate source entries", file_name=file_name)
            rows[file_name] = _AuthorizationRow(
                file_name=file_name, source_id=source_id,
                expected_sha256=value["expected_sha256"], authorized_by=value["authorized_by"],
                authorized_at=value["authorized_at"], license_note=value["license_note"],
                legacy_hash=legacy_hash,
            )
            source_ids.add(source_id)
    except DomainError:
        raise
    except (UnicodeError, csv.Error) as error:
        raise _domain("source authorization manifest could not be read", reason=type(error).__name__) from error
    return rows


def _validate_row(row: Mapping[str, str], *, legacy_hash: bool) -> None:
    if set(row) != set(_AUTHORIZATION_FIELDS):
        raise _domain("authorization manifest row is invalid")
    for field in ("file_name", "source_id", "authorized_by", "authorized_at", "license_note"):
        if not row.get(field, "").strip():
            raise _domain("authorization manifest requires non-empty fields", file_name=row.get("file_name") or None)
    file_name = row["file_name"]
    if not _is_safe_path_segment(file_name):
        raise _domain("authorization file name must be a safe basename", file_name=file_name)
    if not _is_safe_path_segment(row["source_id"]):
        raise _domain("authorization source identifier is unsafe", file_name=file_name)
    expected = row["expected_sha256"]
    if (not legacy_hash and not expected) or (expected and not _SHA256_RE.fullmatch(expected)):
        raise _domain("authorization hash must be lowercase SHA-256", file_name=file_name)
    try:
        authorized_at = datetime.fromisoformat(row["authorized_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise _domain("authorization time is invalid", file_name=file_name) from error
    if authorized_at.tzinfo is None:
        raise _domain("authorization time must include a timezone", file_name=file_name)
