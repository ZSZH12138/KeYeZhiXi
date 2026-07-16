"""M6 repository boundary for tutoring session state history."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.tutoring import SessionStateSnapshot


_SESSION_TABLE = "m6_session_states"


class M6Repository(Protocol):
    """Persistence operations owned exclusively by M6."""

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        """Persist one tutoring turn snapshot."""

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        """Load one exact tutoring turn snapshot."""
