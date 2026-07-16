"""M5 repository boundary for learner and class state histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
)


_LEARNER_TABLE = "m5_learner_states"
_CLASS_TABLE = "m5_class_states"


class M5Repository(Protocol):
    """Persistence operations owned exclusively by M5."""

    def save_learner_state(self, snapshot: LearnerStateSnapshot) -> None:
        """Persist one versioned learner-state snapshot."""

    def get_learner_state(
        self,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Load one exact learner-state version."""

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        """Persist one class-state aggregate."""

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        """Load one class-state aggregate by snapshot identity."""
