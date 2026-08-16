"""UTC clock boundary for production M8 timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return one timezone-aware instant."""


class SystemUTCClock:
    """Read real wall-clock time in UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


__all__ = ["Clock", "SystemUTCClock"]
