"""M5 repository boundary for learner and class state histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
)


_LEARNER_TABLE = "m5_learner_states"
_CLASS_TABLE = "m5_class_states"
_PROCESSED_AUDITS_TABLE = "m5_processed_audits"


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

    def get_latest_learner_state(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        """Load the highest-version learner-state snapshot for restart recovery."""

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        """Persist one class-state aggregate."""

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        """Load one class-state aggregate by snapshot identity."""

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        """Load the most recent class-state aggregate for restart recovery."""

    def get_processed_audits(self, learner_id: str) -> frozenset[str]:
        """Load the set of audit keys already processed for one learner."""

    def save_processed_audits(
        self,
        learner_id: str,
        audit_keys: frozenset[str],
    ) -> None:
        """Persist the full set of processed audit keys for one learner."""
