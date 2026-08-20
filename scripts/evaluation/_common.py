"""Small standard-library helpers shared by offline evaluation scripts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class EvaluationInputError(ValueError):
    """Raised when a local evaluation artifact is malformed or untrusted."""


class _DuplicateKey(ValueError):
    pass


def canonical_json_bytes(value: object) -> bytes:
    """Return one deterministic UTF-8 JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path, *, max_bytes: int | None = None) -> str:
    """Hash a regular, non-symlink file without retaining its contents."""

    checked = require_local_regular_file(path, max_bytes=max_bytes)
    digest = hashlib.sha256()
    with checked.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def require_sha256(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or LOWER_SHA256.fullmatch(value) is None:
        raise EvaluationInputError(f"{field_name} must be a lowercase SHA-256")
    return value


def require_safe_id(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise EvaluationInputError(f"{field_name} is invalid")
    return value


def require_local_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> Path:
    """Reject URLs, relative ambiguity, symlinks, and oversized files."""

    if not isinstance(path, Path):
        raise EvaluationInputError("evaluation inputs must be pathlib.Path values")
    if str(path).lower().startswith(("http://", "https://")):
        raise EvaluationInputError("automatic dataset download is forbidden")
    try:
        if path.is_symlink():
            raise EvaluationInputError("evaluation input symlinks are forbidden")
        resolved = path.resolve(strict=True)
        stat_result = resolved.stat()
    except EvaluationInputError:
        raise
    except OSError:
        raise EvaluationInputError("evaluation input file is unavailable") from None
    if not resolved.is_file():
        raise EvaluationInputError("evaluation input must be a regular file")
    if max_bytes is not None and stat_result.st_size > max_bytes:
        raise EvaluationInputError("evaluation input exceeds its size limit")
    return resolved


def load_json_bytes(payload: bytes, *, field_name: str = "JSON") -> Any:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise EvaluationInputError(f"{field_name} is invalid") from None


def load_json_file(path: Path, *, max_bytes: int, field_name: str) -> Any:
    checked = require_local_regular_file(path, max_bytes=max_bytes)
    try:
        payload = checked.read_bytes()
    except OSError:
        raise EvaluationInputError(f"{field_name} is unavailable") from None
    return load_json_bytes(payload, field_name=field_name)


def load_jsonl_bytes(
    payload: bytes,
    *,
    field_name: str,
    max_records: int,
) -> list[dict[str, Any]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise EvaluationInputError(f"{field_name} must be UTF-8") from None
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        value = load_json_bytes(
            line.encode("utf-8"),
            field_name=f"{field_name} line {line_number}",
        )
        if type(value) is not dict:
            raise EvaluationInputError(
                f"{field_name} line {line_number} must be an object"
            )
        records.append(value)
        if len(records) > max_records:
            raise EvaluationInputError(f"{field_name} contains too many records")
    if not records:
        raise EvaluationInputError(f"{field_name} must not be empty")
    return records


def require_exact_keys(
    value: object,
    expected: set[str] | frozenset[str],
    *,
    field_name: str,
) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(expected):
        raise EvaluationInputError(f"{field_name} has an invalid schema")
    return value


def require_finite_number(
    value: object,
    *,
    field_name: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise EvaluationInputError(f"{field_name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise EvaluationInputError(f"{field_name} must be a finite number")
    if minimum is not None and number < minimum:
        raise EvaluationInputError(f"{field_name} is below its minimum")
    if maximum is not None and number > maximum:
        raise EvaluationInputError(f"{field_name} exceeds its maximum")
    return number


def wilson_interval(
    successes: int,
    total: int,
    *,
    confidence_level: float = 0.95,
) -> dict[str, float | int] | None:
    """Return a Wilson binomial interval for the common 95%/99% levels."""

    if type(successes) is not int or type(total) is not int:
        raise TypeError("binomial counts must be integers")
    if successes < 0 or total < 0 or successes > total:
        raise ValueError("binomial counts are invalid")
    if total == 0:
        return None
    z_by_level = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}
    z = z_by_level.get(round(confidence_level, 2))
    if z is None:
        raise ValueError("supported confidence levels are 0.90, 0.95, and 0.99")
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    radius = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return {
        "successes": successes,
        "total": total,
        "confidence_level": confidence_level,
        "lower": max(0.0, centre - radius),
        "upper": min(1.0, centre + radius),
    }


def percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile values must not be empty")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("percentile probability is invalid")
    ordered = sorted(values)
    location = probability * (len(ordered) - 1)
    lower = int(math.floor(location))
    upper = int(math.ceil(location))
    if lower == upper:
        return ordered[lower]
    weight = location - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def atomic_write_json(path: Path, payload: object) -> None:
    """Write a completed report atomically, never partial provider content."""

    if not isinstance(path, Path):
        raise EvaluationInputError("output path must be a pathlib.Path")
    parent = path.parent.resolve(strict=True)
    data = canonical_json_bytes(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def checksum_records(records: Iterable[Mapping[str, object]]) -> str:
    return sha256_bytes(canonical_json_bytes(list(records)))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


__all__ = [
    "EvaluationInputError",
    "atomic_write_json",
    "canonical_json_bytes",
    "checksum_records",
    "load_json_bytes",
    "load_json_file",
    "load_jsonl_bytes",
    "percentile",
    "require_exact_keys",
    "require_finite_number",
    "require_local_regular_file",
    "require_safe_id",
    "require_sha256",
    "sha256_bytes",
    "sha256_file",
    "wilson_interval",
]
