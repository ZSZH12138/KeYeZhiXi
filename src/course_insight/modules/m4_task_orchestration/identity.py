"""Canonical M4 business identity hashing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def canonical_idempotency_key(identity: Mapping[str, str | None]) -> str:
    """Hash one fixed-field business identity as canonical UTF-8 JSON."""

    serialized = json.dumps(
        dict(identity),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
