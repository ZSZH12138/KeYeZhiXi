"""M0 repository boundary for persisted learning events and workflow state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Callable, Protocol

from course_insight.contracts.events import LearningEvent
from course_insight.modules.m0_platform.outbox import OutboxRecord
from course_insight.modules.m0_platform.workflow import AssessmentRun


class M0Repository(Protocol):
    """Persistence operations owned exclusively by M0."""

    def initialize(self) -> None:
        """Create or migrate the repository storage."""

    def purge_actor(self, actor_id: str) -> int:
        """Physically delete module-owned rows for one actor."""

    def append_events(
        self,
        events: Sequence[LearningEvent],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Atomically persist events and their outbox records.

        Return newly accepted identifiers followed by identifiers already
        present in storage. Repeated identifiers within one input batch are
        collapsed so acknowledgement partitions remain disjoint.
        """

    def deliver_outbox_records(
        self,
        deliver: Callable[[str, str], None],
    ) -> None:
        """Explicit compatibility delivery outside repository transactions."""

    def claim_outbox_batch(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_until: datetime,
        batch_size: int,
    ) -> tuple[OutboxRecord, ...]:
        """Lease one ordered batch of available or expired records."""

    def mark_outbox_delivered(
        self,
        worker_id: str,
        records: Sequence[tuple[str, int]],
    ) -> tuple[str, ...]:
        """Delete only records still owned at the expected versions."""

    def mark_outbox_failed(
        self,
        worker_id: str,
        event_id: str,
        *,
        expected_version: int,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
        dead: bool,
    ) -> bool:
        """Release one owned record to retry or retain it as dead."""

    def renew_outbox_leases(
        self,
        worker_id: str,
        records: Sequence[tuple[str, int]],
        *,
        now: datetime,
        lease_until: datetime,
    ) -> tuple[str, ...]:
        """Extend unexpired leases owned at the expected versions."""

    def schema_is_current(self) -> bool:
        """Return whether storage has the application schema version."""

    def insert_or_get_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """Insert, replay, or atomically adopt one complete pre-v9 row."""

    def adopt_legacy_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """CAS-fill dependencies on one exact, wholly legacy workflow row."""

    def get_assessment_run(
        self,
        operation_id: str,
    ) -> AssessmentRun | None:
        """Load one exact workflow row as an isolated immutable value."""

    def list_assessment_runs(self) -> tuple[AssessmentRun, ...]:
        """Load every workflow row for offline legacy inventory."""

    def get_assessment_run_by_paper(
        self,
        paper_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        """Load the latest workflow metadata for a paper."""

    def get_assessment_run_by_attempt(
        self,
        attempt_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        """Load the latest workflow metadata for an attempt."""

    def claim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun | None:
        """Claim one pending or explicitly failed workflow row."""

    def renew_assessment_run_lease(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """Extend one unexpired lease without advancing its workflow version."""

    def advance_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        checkpoint: str,
        worker_id: str,
        now: datetime,
        feedback_id: str | None = None,
        report_id: str | None = None,
        error_code: str | None = None,
        scoring_result_checksum: str | None = None,
        state_version: int | None = None,
        previous_state_frozen: bool | None = None,
        previous_learner_snapshot_id: str | None = None,
        previous_learner_state_version: int | None = None,
        previous_class_snapshot_id: str | None = None,
        previous_class_state_version: int | None = None,
        policy_id: str | None = None,
        adapter_id: str | None = None,
        adapter_version: str | None = None,
        artifact_sha256: str | None = None,
        feature_schema_version: str | None = None,
        action_space_version: str | None = None,
        gate_policy_version: str | None = None,
    ) -> AssessmentRun:
        """Advance one claimed workflow row by exactly one legal checkpoint."""

    def reclaim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        """Take over an expired workflow lease without rewinding the checkpoint."""

    def complete_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
    ) -> AssessmentRun:
        """Complete a workflow and release its lease."""

    def fail_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        now: datetime,
    ) -> AssessmentRun:
        """Fail a workflow at its last checkpoint and release its lease."""

    def finish_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
        scoring_result_checksum: str | None = None,
        state_version: int | None = None,
        feedback_id: str | None = None,
        report_id: str | None = None,
    ) -> AssessmentRun:
        """Complete a waiting or intermediate workflow without extra posting."""

    def park_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        status: str,
        now: datetime,
        previous_state_frozen: bool | None = None,
        previous_learner_snapshot_id: str | None = None,
        previous_learner_state_version: int | None = None,
        previous_class_snapshot_id: str | None = None,
        previous_class_state_version: int | None = None,
        scoring_result_checksum: str | None = None,
    ) -> AssessmentRun:
        """Release the lease into a teacher-waiting room without posting."""

    def resume_parked_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        """Reclaim one waiting-room row so accepted scores can be posted."""
