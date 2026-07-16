"""Append-only canonical JSONL audit logging."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from course_insight.infrastructure.json_io import dumps_json


def append_json_log(
    path: str | os.PathLike[str],
    record: Mapping[str, Any],
) -> None:
    """Durably append one canonical UTF-8 JSON object as a single line."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = dumps_json(dict(record))
    with target.open(mode="a", encoding="utf-8", newline="") as output:
        output.write(f"{line}\n")
        output.flush()
        os.fsync(output.fileno())
