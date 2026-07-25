"""M5 repository boundary for learner and class state histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
    StateUpdateResult,
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

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Load one learner-state version in its complete teaching scope."""

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        """Persist one class-state aggregate."""

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        """Load one class-state aggregate by snapshot identity."""

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ) -> ClassStateSnapshot | None:
        """Load one class snapshot identity in its complete teaching scope."""

    def get_class_state_exact(
        self,
        course_id: str,
        class_id: str,
        state_version: int,
    ) -> ClassStateSnapshot | None:
        """Load one class-state version in its complete teaching scope."""

    def insert_or_get_state_update(
        self,
        result: StateUpdateResult,
    ) -> StateUpdateResult:
        """Persist or recover one complete attempt-bound update."""

    def get_state_update(self, attempt_id: str) -> StateUpdateResult | None:
        """Load one complete state result by assessment attempt."""

    def get_state_update_version(
        self,
        attempt_id: str,
        state_version: int,
    ) -> StateUpdateResult | None:
        """Load one exact attempt-bound state version."""

    def get_state_update_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> StateUpdateResult | None:
        """Load the earliest state version containing an audit version."""

    def get_latest_learner_state(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        """Load the latest learner state in an exact course/class scope."""

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        """Load the latest class aggregate in an exact course scope."""

    def get_processed_audit_ids(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> frozenset[str]:
        """Load the durable duplicate-processing watermark."""
