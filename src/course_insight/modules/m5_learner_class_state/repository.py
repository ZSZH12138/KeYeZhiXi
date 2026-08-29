"""M5 repository boundary for learner and class state histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.learning_models import (
    BktModelArtifact,
    DinaModelArtifact,
    KnowledgeTraceSnapshot,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
    StateUpdateResult,
)


_LEARNER_TABLE = "m5_learner_states"
_CLASS_TABLE = "m5_class_states"


class M5Repository(Protocol):
    """Persistence operations owned exclusively by M5."""

    def insert_or_get_learning_observation_batch(
        self,
        batch: LearningObservationBatch,
    ) -> LearningObservationBatch:
        """Persist every immutable observation in one replay-safe batch."""

    def list_learning_observations(
        self,
        *,
        course_id: str,
        class_id: str,
    ) -> list[LearningObservation]:
        """List governed observations in deterministic event order."""

    def insert_or_get_dina_model(
        self,
        model: DinaModelArtifact,
    ) -> DinaModelArtifact:
        """Persist or recover one append-only DINA model version."""

    def get_dina_model(
        self,
        *,
        course_id: str,
        model_version: str,
    ) -> DinaModelArtifact | None:
        """Load one exact course-scoped DINA model version."""

    def get_latest_dina_model(
        self,
        *,
        course_id: str,
        class_id: str,
    ) -> DinaModelArtifact | None:
        """Load the latest DINA model for one teaching scope."""

    def insert_or_get_bkt_model(
        self,
        model: BktModelArtifact,
    ) -> BktModelArtifact:
        """Persist or recover one append-only BKT model version."""

    def get_bkt_model(
        self,
        *,
        course_id: str,
        model_version: str,
    ) -> BktModelArtifact | None:
        """Load one exact course-scoped BKT model version."""

    def get_latest_bkt_model(
        self,
        *,
        course_id: str,
        class_id: str,
    ) -> BktModelArtifact | None:
        """Load the latest BKT model for one teaching scope."""

    def insert_or_get_knowledge_trace(
        self,
        trace: KnowledgeTraceSnapshot,
    ) -> KnowledgeTraceSnapshot:
        """Persist or recover one immutable learner trace snapshot."""

    def get_knowledge_trace(
        self,
        *,
        trace_id: str,
    ) -> KnowledgeTraceSnapshot | None:
        """Load one exact BKT knowledge-trace snapshot."""

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
        *,
        expected_previous_class_snapshot_id: str | None = None,
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

    def list_latest_learner_states(
        self,
        course_id: str,
        class_id: str,
    ) -> list[LearnerStateSnapshot]:
        """Load one latest state for every learner in a teaching scope."""

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        """Load the latest class aggregate in an exact course scope."""

    def get_latest_class_state_version(
        self,
        course_id: str,
        class_id: str,
    ) -> int | None:
        """Load the latest internal class history version."""

    def get_processed_audit_ids(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> frozenset[str]:
        """Load the durable duplicate-processing watermark."""
