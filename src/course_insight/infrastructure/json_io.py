"""Strict canonical JSON file boundaries."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def _validate_json_tree(value: Any, *, active_ids: set[int]) -> None:
    if value is None or type(value) in {bool, str, int}:
        if isinstance(value, str):
            value.encode("utf-8")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return
    if type(value) not in {list, dict}:
        raise ValueError("value must contain only lossless JSON types")
    identity = id(value)
    if identity in active_ids:
        raise ValueError("JSON values must not contain recursive containers")
    active_ids.add(identity)
    try:
        if type(value) is list:
            for item in value:
                _validate_json_tree(item, active_ids=active_ids)
            return
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        for key, item in value.items():
            key.encode("utf-8")
            _validate_json_tree(item, active_ids=active_ids)
    finally:
        active_ids.remove(identity)


def dumps_json(value: Any, *, indent: int | None = None) -> str:
    """Serialize a lossless JSON tree with stable key ordering."""

    _validate_json_tree(value, active_ids=set())
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=indent,
            separators=(",", ":") if indent is None else None,
        )
        serialized.encode("utf-8")
    except (OverflowError, TypeError, UnicodeError, ValueError) as error:
        raise ValueError("value must serialize as UTF-8 JSON") from error
    return serialized


def read_json(path: str | os.PathLike[str]) -> Any:
    """Read and validate one UTF-8 JSON document."""

    with Path(path).open(encoding="utf-8") as source:
        value = json.load(source)
    _validate_json_tree(value, active_ids=set())
    return value


def write_json(path: str | os.PathLike[str], value: Any) -> None:
    """Atomically write stable indented UTF-8 JSON with a final newline."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = f"{dumps_json(value, indent=2)}\n"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
            newline="",
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
