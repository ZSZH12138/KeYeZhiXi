"""Deterministic zero-argument M5 service stub."""

from typing import cast

from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
)
from course_insight.modules.m5_learner_class_state.repository import M5Repository
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
)


class InMemoryM5Repository:
    """In-process M5 repository for tests and deterministic integration."""

    def __init__(self) -> None:
        self._learner_states: dict[tuple[str, int], LearnerStateSnapshot] = {}
        self._class_states: dict[str, ClassStateSnapshot] = {}
        self._processed_audits: dict[str, frozenset[str]] = {}

    def save_learner_state(self, snapshot: LearnerStateSnapshot) -> None:
        self._learner_states[
            (snapshot.learner_id, snapshot.state_version)
        ] = snapshot.model_copy(deep=True)

    def get_learner_state(
        self,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        record = self._learner_states.get((learner_id, state_version))
        return record.model_copy(deep=True) if record is not None else None

    def get_latest_learner_state(
        self,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        candidates = [
            snap
            for (lid, _ver), snap in self._learner_states.items()
            if lid == learner_id
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda s: s.state_version)
        return latest.model_copy(deep=True)

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        self._class_states[snapshot.snapshot_id] = snapshot.model_copy(deep=True)

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        record = self._class_states.get(snapshot_id)
        return record.model_copy(deep=True) if record is not None else None

    def get_latest_class_state(
        self,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        candidates = [
            snap
            for snap in self._class_states.values()
            if snap.class_id == class_id
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda s: s.updated_at)
        return latest.model_copy(deep=True)

    def get_processed_audits(self, learner_id: str) -> frozenset[str]:
        return self._processed_audits.get(learner_id, frozenset())

    def save_processed_audits(
        self, learner_id: str, audit_keys: frozenset[str]
    ) -> None:
        self._processed_audits[learner_id] = audit_keys


class M5StateServiceStub(M5StateService):
    """Instantiate M5 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(
            InMemoryM5Repository(),
            DeterministicStateUpdatePolicy(),
            DeterministicClassAggregationPolicy(),
        )
