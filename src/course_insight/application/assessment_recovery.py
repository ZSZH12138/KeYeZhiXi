"""Lease and exact-input controls for restart-safe assessment orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast

from course_insight.application.assessment_dependencies import (
    AssessmentDependencies,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
)
from course_insight.modules.m0_platform.lease_heartbeat import (
    run_with_lease_heartbeat,
)
from course_insight.modules.m0_platform.workflow import (
    WAITING_WORKFLOW_STATUSES,
    AssessmentRun,
)


ResultT = TypeVar("ResultT")


class AssessmentRecovery:
    """Coordinate M0 leases and M5 baseline identities without owning payloads."""

    def __init__(
        self,
        *,
        m0: Any,
        m5: Any,
        now: Callable[[], datetime],
    ) -> None:
        self._m0 = m0
        self._m5 = m5
        self._now = now

    def adopt_legacy_dependencies(
        self,
        run: AssessmentRun,
        dependencies: AssessmentDependencies,
        *,
        frozen_knowledge_bundle_id: str,
        frozen_course_package_id: str,
    ) -> AssessmentRun:
        """CAS-fill dependencies only when the frozen task anchors still match."""

        if (
            dependencies.knowledge_bundle_id
            != frozen_knowledge_bundle_id
            or dependencies.course_package_id != frozen_course_package_id
        ):
            raise DomainError(
                code="WORKFLOW_DEPENDENCY_MISMATCH",
                module="application",
                message="assessment dependencies differ from the frozen task",
                recoverable=True,
            )

        fields = dependencies.as_run_fields()
        if run.operation == "start":
            fields = {
                **fields,
                "evidence_index_id": None,
                "evidence_index_version": None,
                "evidence_index_checksum": None,
                "state_policy_checksum": None,
                "teacher_policy_checksum": None,
            }
        candidate = replace(
            run,
            updated_at=self._now(),
            previous_state_frozen=(
                None
                if run.operation == "start"
                else (
                    False
                    if run.previous_state_frozen is None
                    else run.previous_state_frozen
                )
            ),
            **fields,
        )
        return self._m0.adopt_legacy_assessment_run(candidate)

    def claim(
        self,
        run: AssessmentRun,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> AssessmentRun:
        now = self._now()
        lease_until = now + timedelta(seconds=lease_seconds)
        claimed = self._m0.claim_assessment_run(
            run.operation_id,
            worker_id=worker_id,
            now=now,
            lease_until=lease_until,
        )
        if claimed is not None:
            return claimed
        current = self._m0.get_assessment_run(run.operation_id)
        if (
            current is not None
            and current.status == "running"
            and current.lease_until is not None
            and current.lease_until <= now
        ):
            return self._m0.reclaim_assessment_run(
                run.operation_id,
                worker_id=worker_id,
                now=now,
                lease_until=lease_until,
            )
        raise DomainError(
            code="WORKFLOW_BUSY",
            module="application",
            message="assessment workflow is already being processed",
            recoverable=True,
        )

    def execute(
        self,
        run: AssessmentRun,
        action: Callable[[], ResultT],
        *,
        lease_seconds: float,
        heartbeat_interval_seconds: float,
    ) -> ResultT:
        """Execute one potentially slow step and reject every stale result."""

        if not self._renew(run, lease_seconds):
            raise _lease_lost()
        outcome = run_with_lease_heartbeat(
            action,
            renew=lambda: self._renew(run, lease_seconds),
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        if not outcome.lease_current:
            raise _lease_lost() from outcome.error
        if outcome.error is not None:
            raise outcome.error
        return cast(ResultT, outcome.value)

    def advance(
        self,
        run: AssessmentRun,
        checkpoint: str,
        **refs: object,
    ) -> AssessmentRun:
        return self._m0.advance_assessment_run(
            run.operation_id,
            expected_version=run.version,
            checkpoint=checkpoint,
            worker_id=_owner(run),
            now=self._now(),
            **refs,
        )

    def complete(self, run: AssessmentRun) -> AssessmentRun:
        return self._m0.complete_assessment_run(
            run.operation_id,
            expected_version=run.version,
            worker_id=_owner(run),
            now=self._now(),
        )

    def fail(self, run: AssessmentRun, code: str) -> None:
        """Best-effort failure recording that never writes after ownership loss."""

        if code == "WORKFLOW_LEASE_LOST":
            return
        current = self._m0.get_assessment_run(run.operation_id)
        now = self._now()
        if (
            current is None
            or current.status != "running"
            or current.locked_by != run.locked_by
            or current.lease_until is None
            or current.lease_until <= now
        ):
            return
        try:
            self._m0.fail_assessment_run(
                run.operation_id,
                expected_version=current.version,
                worker_id=_owner(current),
                error_code=code,
                now=now,
            )
        except DomainError:
            return

    def park(
        self,
        run: AssessmentRun,
        status: str,
        **baseline: object,
    ) -> AssessmentRun:
        return self._m0.park_assessment_run(
            run.operation_id,
            expected_version=run.version,
            worker_id=_owner(run),
            status=status,
            now=self._now(),
            **baseline,
        )

    def ensure_waiting(
        self,
        run: AssessmentRun,
        status: str,
        *,
        worker_id: str,
        lease_seconds: float,
        **baseline: object,
    ) -> AssessmentRun:
        """Park a scored submit into a waiting room, including crash replay."""

        current = self._m0.get_assessment_run(run.operation_id)
        if current is None:
            raise DomainError(
                code="WORKFLOW_NOT_FOUND",
                module="application",
                message="assessment workflow row does not exist",
                recoverable=True,
            )
        expected_checksum = baseline.get("scoring_result_checksum")
        if current.status == status and (
            expected_checksum is None
            or current.scoring_result_checksum == expected_checksum
        ):
            return current
        if current.status in WAITING_WORKFLOW_STATUSES:
            current = self.resume_parked(
                current,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
        elif current.status == "failed":
            current = self.claim(current, worker_id, lease_seconds=lease_seconds)
        elif current.status == "running":
            now = self._now()
            if current.lease_until is not None and current.lease_until <= now:
                current = self._m0.reclaim_assessment_run(
                    current.operation_id,
                    worker_id=worker_id,
                    now=now,
                    lease_until=now + timedelta(seconds=lease_seconds),
                )
            elif current.locked_by != worker_id:
                raise DomainError(
                    code="WORKFLOW_BUSY",
                    module="application",
                    message="assessment workflow is already being processed",
                    recoverable=True,
                )
        else:
            raise DomainError(
                code="RESCORE_NOT_ALLOWED",
                module="application",
                message="scored attempt cannot be returned to the waiting room",
                recoverable=True,
            )
        return self.park(current, status, **baseline)

    def resume_parked(
        self,
        run: AssessmentRun,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> AssessmentRun:
        return self._m0.resume_parked_assessment_run(
            run.operation_id,
            worker_id=worker_id,
            now=self._now(),
            lease_until=self._now() + timedelta(seconds=lease_seconds),
        )

    def finish(self, run: AssessmentRun, **refs: object) -> AssessmentRun:
        return self._m0.finish_assessment_run(
            run.operation_id,
            expected_version=run.version,
            worker_id=_owner(run),
            now=self._now(),
            **refs,
        )

    def freeze_state_inputs(
        self,
        run: AssessmentRun,
        *,
        learner: LearnerStateSnapshot | None,
        class_state: ClassStateSnapshot | None,
    ) -> AssessmentRun:
        if run.previous_state_frozen is None:
            raise DomainError(
                code="WORKFLOW_RECOVERY_UNFROZEN_STATE_BASELINE",
                module="application",
                message="legacy workflow state baseline cannot be reconstructed",
                recoverable=True,
            )
        if run.previous_state_frozen:
            return run
        return self.advance(
            run,
            "state_inputs_frozen",
            previous_state_frozen=True,
            previous_learner_snapshot_id=(
                None if learner is None else learner.snapshot_id
            ),
            previous_learner_state_version=(
                None if learner is None else learner.state_version
            ),
            previous_class_snapshot_id=(
                None if class_state is None else class_state.snapshot_id
            ),
            previous_class_state_version=None,
        )

    def rebase_review_state_inputs(
        self,
        run: AssessmentRun,
        *,
        learner: LearnerStateSnapshot | None,
        class_state: ClassStateSnapshot | None,
    ) -> AssessmentRun:
        """Persist the current baseline when retrying an unapplied failed review."""

        return self._m0.rebase_review_state_inputs(
            run.operation_id,
            expected_version=run.version,
            worker_id=_owner(run),
            now=self._now(),
            previous_learner_snapshot_id=(
                None if learner is None else learner.snapshot_id
            ),
            previous_learner_state_version=(
                None if learner is None else learner.state_version
            ),
            previous_class_snapshot_id=(
                None if class_state is None else class_state.snapshot_id
            ),
        )

    def load_state_inputs(
        self,
        run: AssessmentRun,
    ) -> tuple[LearnerStateSnapshot | None, ClassStateSnapshot | None]:
        if run.previous_state_frozen is not True:
            raise DomainError(
                code="WORKFLOW_RECOVERY_UNFROZEN_STATE_BASELINE",
                module="application",
                message="workflow state baseline was not frozen",
                recoverable=True,
            )
        learner = self._load_learner(run)
        class_state = self._load_class(run)
        return learner, class_state

    def _load_learner(
        self,
        run: AssessmentRun,
    ) -> LearnerStateSnapshot | None:
        snapshot_id = run.previous_learner_snapshot_id
        state_version = run.previous_learner_state_version
        if snapshot_id is None and state_version is None:
            return None
        if snapshot_id is None or state_version is None:
            raise _recovery_input_missing()
        snapshot = self._m5.get_learner_state_exact(
            run.course_id,
            run.class_id,
            run.learner_id,
            state_version,
        )
        if snapshot is None or (
            snapshot.snapshot_id,
            snapshot.course_id,
            snapshot.class_id,
            snapshot.learner_id,
            snapshot.state_version,
        ) != (
            snapshot_id,
            run.course_id,
            run.class_id,
            run.learner_id,
            state_version,
        ):
            raise _recovery_input_missing()
        return snapshot

    def _load_class(
        self,
        run: AssessmentRun,
    ) -> ClassStateSnapshot | None:
        snapshot_id = run.previous_class_snapshot_id
        if snapshot_id is None:
            return None
        snapshot = self._m5.get_class_state_by_identity(
            run.course_id,
            run.class_id,
            snapshot_id,
        )
        if snapshot is None or (
            snapshot.snapshot_id,
            snapshot.course_id,
            snapshot.class_id,
        ) != (
            snapshot_id,
            run.course_id,
            run.class_id,
        ):
            raise _recovery_input_missing()
        return snapshot

    def _renew(self, run: AssessmentRun, lease_seconds: float) -> bool:
        now = self._now()
        return (
            self._m0.renew_assessment_run_lease(
                run.operation_id,
                expected_version=run.version,
                worker_id=_owner(run),
                now=now,
                lease_until=now + timedelta(seconds=lease_seconds),
            )
            is True
        )


def ensure_start_dependencies(
    start: AssessmentRun,
    current: AssessmentDependencies,
) -> None:
    """Require the operation to use the bundle frozen by assessment start."""

    expected = (
        start.knowledge_bundle_id,
        start.knowledge_bundle_version,
        start.knowledge_bundle_checksum,
        start.course_package_id,
    )
    actual = (
        current.knowledge_bundle_id,
        current.knowledge_bundle_version,
        current.knowledge_bundle_checksum,
        current.course_package_id,
    )
    if any(value is None for value in expected):
        raise DomainError(
            code="WORKFLOW_DEPENDENCY_UNAVAILABLE",
            module="application",
            message="assessment start dependency identity is unavailable",
            recoverable=True,
        )
    if expected != actual:
        raise DomainError(
            code="WORKFLOW_DEPENDENCY_MISMATCH",
            module="application",
            message="assessment dependencies differ from the frozen start",
            recoverable=True,
        )


def dependencies_from_run(run: AssessmentRun) -> AssessmentDependencies:
    """Decode required operation dependencies from private M0 metadata."""

    required = (
        run.knowledge_bundle_id,
        run.knowledge_bundle_version,
        run.knowledge_bundle_checksum,
        run.course_package_id,
    )
    if any(value is None for value in required):
        raise DomainError(
            code="WORKFLOW_DEPENDENCY_UNAVAILABLE",
            module="application",
            message="assessment dependency identity is unavailable",
            recoverable=True,
        )
    return AssessmentDependencies(
        knowledge_bundle_id=cast(str, run.knowledge_bundle_id),
        knowledge_bundle_version=cast(str, run.knowledge_bundle_version),
        knowledge_bundle_checksum=cast(str, run.knowledge_bundle_checksum),
        course_package_id=cast(str, run.course_package_id),
        evidence_index_id=run.evidence_index_id,
        evidence_index_version=run.evidence_index_version,
        evidence_index_checksum=run.evidence_index_checksum,
        state_policy_checksum=run.state_policy_checksum,
        teacher_policy_checksum=run.teacher_policy_checksum,
        class_roster_size=run.class_roster_size,
        class_roster_checksum=run.class_roster_checksum,
        class_roster_captured_at=run.class_roster_captured_at,
    )


def _owner(run: AssessmentRun) -> str:
    if run.locked_by is None:
        raise DomainError(
            code="WORKFLOW_VERSION_CONFLICT",
            module="application",
            message="assessment workflow is not owned",
            recoverable=True,
        )
    return run.locked_by


def _lease_lost() -> DomainError:
    return DomainError(
        code="WORKFLOW_LEASE_LOST",
        module="application",
        message="assessment workflow lease ownership was lost",
        recoverable=True,
    )


def _recovery_input_missing() -> DomainError:
    return DomainError(
        code="WORKFLOW_RECOVERY_INPUT_MISSING",
        module="application",
        message="frozen assessment recovery input is unavailable",
        recoverable=True,
    )
