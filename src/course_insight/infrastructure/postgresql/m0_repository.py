"""PostgreSQL implementation of the complete M0 repository protocol."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal, cast

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.postgresql.m0_outbox_repository import (
    PostgresM0OutboxRepositoryMixin,
    PostgresM0RepositoryError,
    m0_transaction,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.infrastructure.postgresql.actor_erasure import purge_postgres_actor
from course_insight.modules.m0_platform.workflow import (
    AssessmentRun,
    advance_run,
    assert_assessment_run_replay,
    reconcile_legacy_assessment_run,
)


class PostgresM0Repository(PostgresM0OutboxRepositoryMixin):
    """Persist M0 values without exposing Psycopg connections or rows."""

    def __init__(
        self,
        pool: PostgresPool,
        *,
        outbox_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._outbox_clock = (
            (lambda: datetime.now(timezone.utc))
            if outbox_clock is None
            else outbox_clock
        )

    def initialize(self) -> None:
        """Apply every verified PostgreSQL migration."""

        from course_insight.infrastructure.postgresql.migration_runner import (
            run_migrations,
        )

        run_migrations(self._pool)

    def purge_actor(self, actor_id: str) -> int:
        return purge_postgres_actor(self._pool, module="m0", actor_id=actor_id)

    def schema_is_current(self) -> bool:
        """Return whether the database is at the latest verified migration."""

        from course_insight.infrastructure.postgresql.migration_runner import (
            schema_is_current,
        )

        return schema_is_current(self._pool)

    def insert_or_get_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """Insert one workflow identity or return its authoritative replay."""

        candidate = _isolated_run(run)
        with m0_transaction(self._pool) as connection:
            inserted = connection.execute(
                f"""
                INSERT INTO m0_assessment_runs(
                    operation_id,
                    operation,
                    request_checksum,
                    course_id,
                    class_id,
                    learner_id,
                    session_id,
                    task_id,
                    paper_id,
                    attempt_id,
                    feedback_id,
                    report_id,
                    scoring_result_checksum,
                    target_audit_id,
                    target_audit_version,
                    state_version,
                    knowledge_bundle_id,
                    knowledge_bundle_version,
                    knowledge_bundle_checksum,
                    course_package_id,
                    evidence_index_id,
                    evidence_index_version,
                    evidence_index_checksum,
                    state_policy_checksum,
                    teacher_policy_checksum,
                    class_roster_size,
                    class_roster_checksum,
                    class_roster_captured_at,
                    previous_state_frozen,
                    previous_learner_snapshot_id,
                    previous_learner_state_version,
                    previous_class_snapshot_id,
                    previous_class_state_version,
                    policy_id,
                    adapter_id,
                    adapter_version,
                    artifact_sha256,
                    feature_schema_version,
                    action_space_version,
                    gate_policy_version,
                    checkpoint,
                    status,
                    version,
                    locked_by,
                    lease_until,
                    error_code,
                    created_at,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT DO NOTHING
                RETURNING {_WORKFLOW_COLUMNS}
                """,
                _run_parameters(candidate),
            ).fetchone()
            if inserted is not None:
                authoritative = _run_from_row(inserted)
                _assert_same_run(authoritative, candidate)
                return authoritative

            existing = _workflow_row(
                connection,
                candidate.operation_id,
                for_update=True,
            )
            if existing is not None:
                authoritative = _run_from_row(existing)
                reconciled = reconcile_legacy_assessment_run(
                    authoritative,
                    candidate,
                )
                if reconciled is not authoritative:
                    authoritative = (
                        _adopt_workflow_dependencies(
                            connection,
                            expected=authoritative,
                            updated=reconciled,
                        )
                        if authoritative.knowledge_bundle_id is None
                        else _adopt_workflow_policy(
                            connection,
                            expected=authoritative,
                            updated=reconciled,
                        )
                    )
                return authoritative

            if candidate.operation == "submit":
                paper_row = connection.execute(
                    f"""
                    SELECT {_WORKFLOW_COLUMNS}
                    FROM m0_assessment_runs
                    WHERE operation = 'submit' AND paper_id = %s
                    ORDER BY created_at, operation_id
                    LIMIT 1
                    """,
                    (candidate.paper_id,),
                ).fetchone()
                if paper_row is not None:
                    raise _submission_conflict(
                        "assessment workflow already exists for this paper"
                    )
            if candidate.operation == "review":
                review_row = connection.execute(
                    f"""
                    SELECT {_WORKFLOW_COLUMNS}
                    FROM m0_assessment_runs
                    WHERE operation = 'review'
                      AND status <> 'completed'
                      AND paper_id = %s
                    ORDER BY created_at, operation_id
                    LIMIT 1
                    """,
                    (candidate.paper_id,),
                ).fetchone()
                if review_row is not None:
                    raise _review_busy()
            raise PostgresM0RepositoryError(
                "PostgreSQL M0 repository operation failed"
            )

    def adopt_legacy_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """CAS-fill dependencies on one exact, wholly legacy workflow row."""

        candidate = _isolated_run(run)
        with m0_transaction(self._pool) as connection:
            row = _workflow_row(
                connection,
                candidate.operation_id,
                for_update=True,
            )
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
            reconciled = reconcile_legacy_assessment_run(
                current,
                candidate,
            )
            if reconciled is current:
                return current
            return (
                _adopt_workflow_dependencies(
                    connection,
                    expected=current,
                    updated=reconciled,
                )
                if current.knowledge_bundle_id is None
                else _adopt_workflow_policy(
                    connection,
                    expected=current,
                    updated=reconciled,
                )
            )

    def get_assessment_run(
        self,
        operation_id: str,
    ) -> AssessmentRun | None:
        """Load one exact workflow row as an immutable domain value."""

        with m0_transaction(self._pool) as connection:
            row = _workflow_row(connection, operation_id)
            return None if row is None else _run_from_row(row)

    def list_assessment_runs(self) -> tuple[AssessmentRun, ...]:
        """Load every workflow row for offline legacy inventory."""

        with m0_transaction(self._pool) as connection:
            rows = connection.execute(
                f"""
                SELECT {_WORKFLOW_COLUMNS}
                FROM m0_assessment_runs
                ORDER BY operation_id
                """
            ).fetchall()
            return tuple(_run_from_row(row) for row in rows)

    def get_assessment_run_by_paper(
        self,
        paper_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        """Load the latest workflow metadata for a paper."""

        return self._get_assessment_run_by_reference(
            "paper_id",
            paper_id,
            operation=operation,
            status=status,
        )

    def get_assessment_run_by_attempt(
        self,
        attempt_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        """Load the latest workflow metadata for an attempt."""

        return self._get_assessment_run_by_reference(
            "attempt_id",
            attempt_id,
            operation=operation,
            status=status,
        )

    def _get_assessment_run_by_reference(
        self,
        column: Literal["paper_id", "attempt_id"],
        value: str,
        *,
        operation: str | None,
        status: str | None,
    ) -> AssessmentRun | None:
        if column not in {"paper_id", "attempt_id"}:
            raise ValueError("assessment reference column is invalid")
        filters = [f"{column} = %s"]
        parameters: list[object] = [value]
        if operation is not None:
            filters.append("operation = %s")
            parameters.append(operation)
        if status is not None:
            filters.append("status = %s")
            parameters.append(status)
        with m0_transaction(self._pool) as connection:
            row = connection.execute(
                f"""
                SELECT {_WORKFLOW_COLUMNS}
                FROM m0_assessment_runs
                WHERE {" AND ".join(filters)}
                ORDER BY updated_at DESC, created_at DESC, operation_id DESC
                LIMIT 1
                """,
                tuple(parameters),
            ).fetchone()
            return None if row is None else _run_from_row(row)

    def claim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun | None:
        """Claim a pending or explicitly failed workflow row."""

        with m0_transaction(self._pool) as connection:
            row = _workflow_row(connection, operation_id, for_update=True)
            if row is None:
                return None
            current = _run_from_row(row)
            if current.status not in {"pending", "failed"}:
                return None
            if current.status == "pending":
                claimed = advance_run(
                    current,
                    "claimed",
                    now=now,
                    locked_by=worker_id,
                    lease_until=lease_until,
                )
            else:
                claimed = replace(
                    current,
                    status="running",
                    version=current.version + 1,
                    locked_by=worker_id,
                    lease_until=lease_until,
                    error_code=None,
                    updated_at=now,
                )
            _update_workflow_row(
                connection,
                expected=current,
                updated=claimed,
            )
            return claimed

    def renew_assessment_run_lease(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> bool:
        """Renew an unexpired owned lease without changing workflow version."""

        if lease_until <= now:
            raise ValueError("lease_until must follow now")
        with m0_transaction(self._pool) as connection:
            row = connection.execute(
                """
                UPDATE m0_assessment_runs
                SET lease_until = %s, updated_at = %s
                WHERE operation_id = %s
                  AND status = 'running'
                  AND locked_by = %s
                  AND version = %s
                  AND lease_until > %s
                RETURNING operation_id
                """,
                (
                    lease_until,
                    now,
                    operation_id,
                    worker_id,
                    expected_version,
                    now,
                ),
            ).fetchone()
            return row is not None

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
        """Advance an owned workflow by its one legal domain checkpoint."""

        with m0_transaction(self._pool) as connection:
            current = _required_locked_run(connection, operation_id)
            _require_owner_version(
                current,
                expected_version=expected_version,
                worker_id=worker_id,
            )
            _require_live_lease(current, now=now)
            _validate_state_input_transition(
                current,
                checkpoint=checkpoint,
                previous_state_frozen=previous_state_frozen,
                previous_learner_snapshot_id=previous_learner_snapshot_id,
                previous_learner_state_version=previous_learner_state_version,
                previous_class_snapshot_id=previous_class_snapshot_id,
                previous_class_state_version=previous_class_state_version,
            )
            _validate_policy_transition(
                current,
                checkpoint=checkpoint,
                policy_id=policy_id,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                artifact_sha256=artifact_sha256,
                feature_schema_version=feature_schema_version,
                action_space_version=action_space_version,
                gate_policy_version=gate_policy_version,
            )
            completing = checkpoint == "completed"
            updated = advance_run(
                current,
                checkpoint,
                now=now,
                feedback_id=feedback_id or current.feedback_id,
                report_id=report_id or current.report_id,
                scoring_result_checksum=(
                    scoring_result_checksum
                    or current.scoring_result_checksum
                ),
                state_version=state_version or current.state_version,
                previous_state_frozen=(
                    current.previous_state_frozen
                    if previous_state_frozen is None
                    else previous_state_frozen
                ),
                previous_learner_snapshot_id=(
                    previous_learner_snapshot_id
                    or current.previous_learner_snapshot_id
                ),
                previous_learner_state_version=(
                    previous_learner_state_version
                    or current.previous_learner_state_version
                ),
                previous_class_snapshot_id=(
                    previous_class_snapshot_id
                    or current.previous_class_snapshot_id
                ),
                previous_class_state_version=(
                    previous_class_state_version
                    or current.previous_class_state_version
                ),
                policy_id=policy_id or current.policy_id,
                adapter_id=adapter_id or current.adapter_id,
                adapter_version=adapter_version or current.adapter_version,
                artifact_sha256=(
                    artifact_sha256
                    if policy_id is not None
                    else current.artifact_sha256
                ),
                feature_schema_version=(
                    feature_schema_version or current.feature_schema_version
                ),
                action_space_version=(
                    action_space_version or current.action_space_version
                ),
                gate_policy_version=(
                    gate_policy_version or current.gate_policy_version
                ),
                error_code=error_code,
                locked_by=None if completing else current.locked_by,
                lease_until=None if completing else current.lease_until,
            )
            _update_workflow_row(
                connection,
                expected=current,
                updated=updated,
            )
            return updated

    def reclaim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        """Take over an expired lease without rewinding its checkpoint."""

        with m0_transaction(self._pool) as connection:
            current = _required_locked_run(connection, operation_id)
            if (
                current.status != "running"
                or current.lease_until is None
                or current.lease_until > now
            ):
                raise DomainError(
                    code="WORKFLOW_LEASE_ACTIVE",
                    module="m0",
                    message="assessment workflow lease has not expired",
                    recoverable=True,
                )
            reclaimed = replace(
                current,
                version=current.version + 1,
                locked_by=worker_id,
                lease_until=lease_until,
                updated_at=now,
            )
            _update_workflow_row(
                connection,
                expected=current,
                updated=reclaimed,
            )
            return reclaimed

    def complete_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
    ) -> AssessmentRun:
        """Complete the next terminal transition and release ownership."""

        return self.advance_assessment_run(
            operation_id,
            expected_version=expected_version,
            checkpoint="completed",
            worker_id=worker_id,
            now=now,
        )

    def fail_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        error_code: str,
        now: datetime,
    ) -> AssessmentRun:
        """Fail at the current checkpoint and release the workflow lease."""

        with m0_transaction(self._pool) as connection:
            current = _required_locked_run(connection, operation_id)
            _require_owner_version(
                current,
                expected_version=expected_version,
                worker_id=worker_id,
            )
            if current.status != "running":
                raise _version_conflict()
            _require_live_lease(current, now=now)
            failed = replace(
                current,
                status="failed",
                version=current.version + 1,
                locked_by=None,
                lease_until=None,
                error_code=error_code,
                updated_at=now,
            )
            _update_workflow_row(
                connection,
                expected=current,
                updated=failed,
            )
            return failed

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
        """Release the lease into a teacher waiting room."""

        from course_insight.modules.m0_platform.workflow import (
            WAITING_WORKFLOW_STATUSES,
        )

        with m0_transaction(self._pool) as connection:
            current = _required_locked_run(connection, operation_id)
            _require_owner_version(
                current,
                expected_version=expected_version,
                worker_id=worker_id,
            )
            if (
                current.status != "running"
                or status not in WAITING_WORKFLOW_STATUSES
            ):
                raise _version_conflict()
            _require_live_lease(current, now=now)
            parked = replace(
                current,
                status=status,  # type: ignore[arg-type]
                version=current.version + 1,
                locked_by=None,
                lease_until=None,
                updated_at=now,
                previous_state_frozen=(
                    current.previous_state_frozen
                    if previous_state_frozen is None
                    else previous_state_frozen
                ),
                previous_learner_snapshot_id=(
                    current.previous_learner_snapshot_id
                    if previous_learner_snapshot_id is None
                    else previous_learner_snapshot_id
                ),
                previous_learner_state_version=(
                    current.previous_learner_state_version
                    if previous_learner_state_version is None
                    else previous_learner_state_version
                ),
                previous_class_snapshot_id=(
                    current.previous_class_snapshot_id
                    if previous_class_snapshot_id is None
                    else previous_class_snapshot_id
                ),
                previous_class_state_version=(
                    current.previous_class_state_version
                    if previous_class_state_version is None
                    else previous_class_state_version
                ),
                scoring_result_checksum=(
                    current.scoring_result_checksum
                    if scoring_result_checksum is None
                    else scoring_result_checksum
                ),
            )
            _update_workflow_row(
                connection,
                expected=current,
                updated=parked,
            )
            return parked

    def resume_parked_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        """Reclaim a waiting-room row so accepted scores can be posted."""

        from course_insight.modules.m0_platform.workflow import (
            WAITING_WORKFLOW_STATUSES,
        )

        with m0_transaction(self._pool) as connection:
            current = _required_locked_run(connection, operation_id)
            if current.status not in WAITING_WORKFLOW_STATUSES:
                raise DomainError(
                    code="WORKFLOW_LEASE_ACTIVE",
                    module="m0",
                    message="assessment workflow is not waiting for review",
                    recoverable=True,
                )
            resumed = replace(
                current,
                status="running",
                version=current.version + 1,
                locked_by=worker_id,
                lease_until=lease_until,
                updated_at=now,
            )
            _update_workflow_row(
                connection,
                expected=current,
                updated=resumed,
            )
            return resumed

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
        """Mark the current checkpoint complete without further posting."""

        with m0_transaction(self._pool) as connection:
            current = _required_locked_run(connection, operation_id)
            _require_owner_version(
                current,
                expected_version=expected_version,
                worker_id=worker_id,
            )
            if current.status != "running":
                raise _version_conflict()
            _require_live_lease(current, now=now)
            finished = replace(
                current,
                checkpoint="completed",
                status="completed",
                version=current.version + 1,
                locked_by=None,
                lease_until=None,
                updated_at=now,
                scoring_result_checksum=(
                    current.scoring_result_checksum
                    if scoring_result_checksum is None
                    else scoring_result_checksum
                ),
                state_version=(
                    current.state_version
                    if state_version is None
                    else state_version
                ),
                feedback_id=(
                    current.feedback_id if feedback_id is None else feedback_id
                ),
                report_id=current.report_id if report_id is None else report_id,
            )
            _update_workflow_row(
                connection,
                expected=current,
                updated=finished,
            )
            return finished


_WORKFLOW_COLUMNS = """
operation_id,
operation,
request_checksum,
course_id,
class_id,
learner_id,
session_id,
task_id,
paper_id,
attempt_id,
feedback_id,
report_id,
scoring_result_checksum,
target_audit_id,
target_audit_version,
state_version,
knowledge_bundle_id,
knowledge_bundle_version,
knowledge_bundle_checksum,
course_package_id,
evidence_index_id,
evidence_index_version,
evidence_index_checksum,
state_policy_checksum,
teacher_policy_checksum,
class_roster_size,
class_roster_checksum,
class_roster_captured_at,
previous_state_frozen,
previous_learner_snapshot_id,
previous_learner_state_version,
previous_class_snapshot_id,
previous_class_state_version,
policy_id,
adapter_id,
adapter_version,
artifact_sha256,
feature_schema_version,
action_space_version,
gate_policy_version,
checkpoint,
status,
version,
locked_by,
lease_until,
error_code,
created_at,
updated_at
"""


def _workflow_row(
    connection: object,
    operation_id: str,
    *,
    for_update: bool = False,
) -> Mapping[str, object] | None:
    suffix = "FOR UPDATE" if for_update else ""
    return connection.execute(  # type: ignore[attr-defined,no-any-return]
        f"""
        SELECT {_WORKFLOW_COLUMNS}
        FROM m0_assessment_runs
        WHERE operation_id = %s
        {suffix}
        """,
        (operation_id,),
    ).fetchone()


def _required_locked_run(
    connection: object,
    operation_id: str,
) -> AssessmentRun:
    row = _workflow_row(connection, operation_id, for_update=True)
    if row is None:
        raise DomainError(
            code="WORKFLOW_NOT_FOUND",
            module="m0",
            message="assessment workflow row does not exist",
            recoverable=True,
        )
    return _run_from_row(row)


def _run_parameters(run: AssessmentRun) -> tuple[object, ...]:
    return (
        run.operation_id,
        run.operation,
        run.request_checksum,
        run.course_id,
        run.class_id,
        run.learner_id,
        run.session_id,
        run.task_id,
        run.paper_id,
        run.attempt_id,
        run.feedback_id,
        run.report_id,
        run.scoring_result_checksum,
        run.target_audit_id,
        run.target_audit_version,
        run.state_version,
        run.knowledge_bundle_id,
        run.knowledge_bundle_version,
        run.knowledge_bundle_checksum,
        run.course_package_id,
        run.evidence_index_id,
        run.evidence_index_version,
        run.evidence_index_checksum,
        run.state_policy_checksum,
        run.teacher_policy_checksum,
        run.class_roster_size,
        run.class_roster_checksum,
        run.class_roster_captured_at,
        run.previous_state_frozen,
        run.previous_learner_snapshot_id,
        run.previous_learner_state_version,
        run.previous_class_snapshot_id,
        run.previous_class_state_version,
        run.policy_id,
        run.adapter_id,
        run.adapter_version,
        run.artifact_sha256,
        run.feature_schema_version,
        run.action_space_version,
        run.gate_policy_version,
        run.checkpoint,
        run.status,
        run.version,
        run.locked_by,
        run.lease_until,
        run.error_code,
        run.created_at,
        run.updated_at,
    )


def _update_workflow_row(
    connection: object,
    *,
    expected: AssessmentRun,
    updated: AssessmentRun,
) -> None:
    cursor = connection.execute(  # type: ignore[attr-defined]
        """
        UPDATE m0_assessment_runs
        SET checkpoint = %s,
            status = %s,
            version = %s,
            locked_by = %s,
            lease_until = %s,
            feedback_id = %s,
            report_id = %s,
            scoring_result_checksum = %s,
            state_version = %s,
            previous_state_frozen = %s,
            previous_learner_snapshot_id = %s,
            previous_learner_state_version = %s,
            previous_class_snapshot_id = %s,
            previous_class_state_version = %s,
            policy_id = %s,
            adapter_id = %s,
            adapter_version = %s,
            artifact_sha256 = %s,
            feature_schema_version = %s,
            action_space_version = %s,
            gate_policy_version = %s,
            error_code = %s,
            updated_at = %s
        WHERE operation_id = %s
          AND version = %s
          AND locked_by IS NOT DISTINCT FROM %s
        """,
        (
            updated.checkpoint,
            updated.status,
            updated.version,
            updated.locked_by,
            updated.lease_until,
            updated.feedback_id,
            updated.report_id,
            updated.scoring_result_checksum,
            updated.state_version,
            updated.previous_state_frozen,
            updated.previous_learner_snapshot_id,
            updated.previous_learner_state_version,
            updated.previous_class_snapshot_id,
            updated.previous_class_state_version,
            updated.policy_id,
            updated.adapter_id,
            updated.adapter_version,
            updated.artifact_sha256,
            updated.feature_schema_version,
            updated.action_space_version,
            updated.gate_policy_version,
            updated.error_code,
            updated.updated_at,
            expected.operation_id,
            expected.version,
            expected.locked_by,
        ),
    )
    if cursor.rowcount != 1:
        raise _version_conflict(
            "assessment workflow row changed concurrently"
        )


def _adopt_workflow_dependencies(
    connection: object,
    *,
    expected: AssessmentRun,
    updated: AssessmentRun,
) -> AssessmentRun:
    cursor = connection.execute(  # type: ignore[attr-defined]
        f"""
        UPDATE m0_assessment_runs
        SET knowledge_bundle_id = %s,
            knowledge_bundle_version = %s,
            knowledge_bundle_checksum = %s,
            course_package_id = %s,
            evidence_index_id = %s,
            evidence_index_version = %s,
            evidence_index_checksum = %s,
            state_policy_checksum = %s,
            teacher_policy_checksum = %s,
            class_roster_size = %s,
            class_roster_checksum = %s,
            class_roster_captured_at = %s,
            previous_state_frozen = %s,
            policy_id = %s,
            adapter_id = %s,
            adapter_version = %s,
            artifact_sha256 = %s,
            feature_schema_version = %s,
            action_space_version = %s,
            gate_policy_version = %s,
            version = %s,
            updated_at = %s
        WHERE operation_id = %s
          AND version = %s
          AND knowledge_bundle_id IS NULL
          AND knowledge_bundle_version IS NULL
          AND knowledge_bundle_checksum IS NULL
          AND course_package_id IS NULL
          AND evidence_index_id IS NULL
          AND evidence_index_version IS NULL
          AND evidence_index_checksum IS NULL
          AND state_policy_checksum IS NULL
          AND teacher_policy_checksum IS NULL
          AND class_roster_size IS NULL
          AND class_roster_checksum IS NULL
          AND class_roster_captured_at IS NULL
          AND previous_state_frozen IS NULL
          AND previous_learner_snapshot_id IS NULL
          AND previous_learner_state_version IS NULL
          AND previous_class_snapshot_id IS NULL
          AND previous_class_state_version IS NULL
          AND policy_id IS NULL
          AND adapter_id IS NULL
          AND adapter_version IS NULL
          AND artifact_sha256 IS NULL
          AND feature_schema_version IS NULL
          AND action_space_version IS NULL
          AND gate_policy_version IS NULL
        RETURNING {_WORKFLOW_COLUMNS}
        """,
        (
            updated.knowledge_bundle_id,
            updated.knowledge_bundle_version,
            updated.knowledge_bundle_checksum,
            updated.course_package_id,
            updated.evidence_index_id,
            updated.evidence_index_version,
            updated.evidence_index_checksum,
            updated.state_policy_checksum,
            updated.teacher_policy_checksum,
            updated.class_roster_size,
            updated.class_roster_checksum,
            updated.class_roster_captured_at,
            updated.previous_state_frozen,
            updated.policy_id,
            updated.adapter_id,
            updated.adapter_version,
            updated.artifact_sha256,
            updated.feature_schema_version,
            updated.action_space_version,
            updated.gate_policy_version,
            updated.version,
            updated.updated_at,
            expected.operation_id,
            expected.version,
        ),
    )
    row = cursor.fetchone()
    if cursor.rowcount != 1 or row is None:
        raise _version_conflict(
            "assessment workflow row changed concurrently"
        )
    return _run_from_row(row)


def _adopt_workflow_policy(
    connection: object,
    *,
    expected: AssessmentRun,
    updated: AssessmentRun,
) -> AssessmentRun:
    cursor = connection.execute(  # type: ignore[attr-defined]
        f"""
        UPDATE m0_assessment_runs
        SET policy_id = %s,
            adapter_id = %s,
            adapter_version = %s,
            artifact_sha256 = %s,
            feature_schema_version = %s,
            action_space_version = %s,
            gate_policy_version = %s,
            version = %s,
            updated_at = %s
        WHERE operation_id = %s
          AND version = %s
          AND policy_id IS NULL
          AND adapter_id IS NULL
          AND adapter_version IS NULL
          AND artifact_sha256 IS NULL
          AND feature_schema_version IS NULL
          AND action_space_version IS NULL
          AND gate_policy_version IS NULL
        RETURNING {_WORKFLOW_COLUMNS}
        """,
        (
            updated.policy_id,
            updated.adapter_id,
            updated.adapter_version,
            updated.artifact_sha256,
            updated.feature_schema_version,
            updated.action_space_version,
            updated.gate_policy_version,
            updated.version,
            updated.updated_at,
            expected.operation_id,
            expected.version,
        ),
    )
    row = cursor.fetchone()
    if cursor.rowcount != 1 or row is None:
        raise _version_conflict(
            "assessment workflow row changed concurrently"
        )
    return _run_from_row(row)


def _run_from_row(row: Mapping[str, object]) -> AssessmentRun:
    try:
        operation = _required_text(row["operation"], field="operation")
        status = _required_text(row["status"], field="status")
        if operation not in {"start", "submit", "review", "rescore"}:
            raise ValueError("operation is invalid")
        if status not in {
            "pending",
            "running",
            "failed",
            "completed",
            "awaiting_review",
            "awaiting_rescore",
        }:
            raise ValueError("status is invalid")
        return AssessmentRun(
            operation_id=_required_text(
                row["operation_id"],
                field="operation_id",
            ),
            operation=cast(Any, operation),
            request_checksum=_required_text(
                row["request_checksum"],
                field="request_checksum",
            ),
            course_id=_required_text(row["course_id"], field="course_id"),
            class_id=_required_text(row["class_id"], field="class_id"),
            learner_id=_required_text(row["learner_id"], field="learner_id"),
            session_id=_required_text(row["session_id"], field="session_id"),
            task_id=_required_text(row["task_id"], field="task_id"),
            paper_id=_required_text(row["paper_id"], field="paper_id"),
            attempt_id=_optional_text(row["attempt_id"], field="attempt_id"),
            feedback_id=_optional_text(
                row["feedback_id"],
                field="feedback_id",
            ),
            report_id=_optional_text(row["report_id"], field="report_id"),
            scoring_result_checksum=_optional_text(
                row["scoring_result_checksum"],
                field="scoring_result_checksum",
            ),
            target_audit_id=_optional_text(
                row["target_audit_id"],
                field="target_audit_id",
            ),
            target_audit_version=_optional_positive_int(
                row["target_audit_version"],
                field="target_audit_version",
            ),
            state_version=_optional_positive_int(
                row["state_version"],
                field="state_version",
            ),
            knowledge_bundle_id=_optional_text(
                row["knowledge_bundle_id"],
                field="knowledge_bundle_id",
            ),
            knowledge_bundle_version=_optional_text(
                row["knowledge_bundle_version"],
                field="knowledge_bundle_version",
            ),
            knowledge_bundle_checksum=_optional_text(
                row["knowledge_bundle_checksum"],
                field="knowledge_bundle_checksum",
            ),
            course_package_id=_optional_text(
                row["course_package_id"],
                field="course_package_id",
            ),
            evidence_index_id=_optional_text(
                row["evidence_index_id"],
                field="evidence_index_id",
            ),
            evidence_index_version=_optional_text(
                row["evidence_index_version"],
                field="evidence_index_version",
            ),
            evidence_index_checksum=_optional_text(
                row["evidence_index_checksum"],
                field="evidence_index_checksum",
            ),
            state_policy_checksum=_optional_text(
                row["state_policy_checksum"],
                field="state_policy_checksum",
            ),
            teacher_policy_checksum=_optional_text(
                row["teacher_policy_checksum"],
                field="teacher_policy_checksum",
            ),
            class_roster_size=_optional_positive_int(
                row["class_roster_size"],
                field="class_roster_size",
            ),
            class_roster_checksum=_optional_text(
                row["class_roster_checksum"],
                field="class_roster_checksum",
            ),
            class_roster_captured_at=_optional_datetime(
                row["class_roster_captured_at"],
                field="class_roster_captured_at",
            ),
            previous_state_frozen=_optional_bool(
                row["previous_state_frozen"],
                field="previous_state_frozen",
            ),
            previous_learner_snapshot_id=_optional_text(
                row["previous_learner_snapshot_id"],
                field="previous_learner_snapshot_id",
            ),
            previous_learner_state_version=_optional_positive_int(
                row["previous_learner_state_version"],
                field="previous_learner_state_version",
            ),
            previous_class_snapshot_id=_optional_text(
                row["previous_class_snapshot_id"],
                field="previous_class_snapshot_id",
            ),
            previous_class_state_version=_optional_positive_int(
                row["previous_class_state_version"],
                field="previous_class_state_version",
            ),
            policy_id=_optional_text(
                row["policy_id"],
                field="policy_id",
            ),
            adapter_id=_optional_text(
                row["adapter_id"],
                field="adapter_id",
            ),
            adapter_version=_optional_text(
                row["adapter_version"],
                field="adapter_version",
            ),
            artifact_sha256=_optional_text(
                row["artifact_sha256"],
                field="artifact_sha256",
            ),
            feature_schema_version=_optional_text(
                row["feature_schema_version"],
                field="feature_schema_version",
            ),
            action_space_version=_optional_text(
                row["action_space_version"],
                field="action_space_version",
            ),
            gate_policy_version=_optional_text(
                row["gate_policy_version"],
                field="gate_policy_version",
            ),
            checkpoint=_required_text(
                row["checkpoint"],
                field="checkpoint",
            ),
            status=cast(Any, status),
            version=_positive_int(row["version"], field="version"),
            locked_by=_optional_text(row["locked_by"], field="locked_by"),
            lease_until=_optional_datetime(
                row["lease_until"],
                field="lease_until",
            ),
            error_code=_optional_text(row["error_code"], field="error_code"),
            created_at=_required_datetime(
                row["created_at"],
                field="created_at",
            ),
            updated_at=_required_datetime(
                row["updated_at"],
                field="updated_at",
            ),
        )
    except (KeyError, TypeError):
        raise ValueError("assessment workflow row is invalid") from None


def _isolated_run(run: AssessmentRun) -> AssessmentRun:
    return _run_from_row(_run_mapping(run))


def _run_mapping(run: AssessmentRun) -> dict[str, object]:
    return {
        "operation_id": run.operation_id,
        "operation": run.operation,
        "request_checksum": run.request_checksum,
        "course_id": run.course_id,
        "class_id": run.class_id,
        "learner_id": run.learner_id,
        "session_id": run.session_id,
        "task_id": run.task_id,
        "paper_id": run.paper_id,
        "attempt_id": run.attempt_id,
        "feedback_id": run.feedback_id,
        "report_id": run.report_id,
        "scoring_result_checksum": run.scoring_result_checksum,
        "target_audit_id": run.target_audit_id,
        "target_audit_version": run.target_audit_version,
        "state_version": run.state_version,
        "knowledge_bundle_id": run.knowledge_bundle_id,
        "knowledge_bundle_version": run.knowledge_bundle_version,
        "knowledge_bundle_checksum": run.knowledge_bundle_checksum,
        "course_package_id": run.course_package_id,
        "evidence_index_id": run.evidence_index_id,
        "evidence_index_version": run.evidence_index_version,
        "evidence_index_checksum": run.evidence_index_checksum,
        "state_policy_checksum": run.state_policy_checksum,
        "teacher_policy_checksum": run.teacher_policy_checksum,
        "class_roster_size": run.class_roster_size,
        "class_roster_checksum": run.class_roster_checksum,
        "class_roster_captured_at": run.class_roster_captured_at,
        "previous_state_frozen": run.previous_state_frozen,
        "previous_learner_snapshot_id": run.previous_learner_snapshot_id,
        "previous_learner_state_version": run.previous_learner_state_version,
        "previous_class_snapshot_id": run.previous_class_snapshot_id,
        "previous_class_state_version": run.previous_class_state_version,
        "policy_id": run.policy_id,
        "adapter_id": run.adapter_id,
        "adapter_version": run.adapter_version,
        "artifact_sha256": run.artifact_sha256,
        "feature_schema_version": run.feature_schema_version,
        "action_space_version": run.action_space_version,
        "gate_policy_version": run.gate_policy_version,
        "checkpoint": run.checkpoint,
        "status": run.status,
        "version": run.version,
        "locked_by": run.locked_by,
        "lease_until": run.lease_until,
        "error_code": run.error_code,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _assert_same_run(left: AssessmentRun, right: AssessmentRun) -> None:
    assert_assessment_run_replay(left, right)


def _require_owner_version(
    current: AssessmentRun,
    *,
    expected_version: int,
    worker_id: str,
) -> None:
    if current.version != expected_version or current.locked_by != worker_id:
        raise _version_conflict()


def _validate_state_input_transition(
    current: AssessmentRun,
    *,
    checkpoint: str,
    previous_state_frozen: bool | None,
    previous_learner_snapshot_id: str | None,
    previous_learner_state_version: int | None,
    previous_class_snapshot_id: str | None,
    previous_class_state_version: int | None,
) -> None:
    supplied = (
        previous_state_frozen is not None
        or previous_learner_snapshot_id is not None
        or previous_learner_state_version is not None
        or previous_class_snapshot_id is not None
        or previous_class_state_version is not None
    )
    if checkpoint == "state_inputs_frozen":
        if (
            current.previous_state_frozen is not False
            or previous_state_frozen is not True
        ):
            raise DomainError(
                code="WORKFLOW_RECOVERY_CONTEXT_MISSING",
                module="m0",
                message="assessment state baseline cannot be frozen safely",
                recoverable=True,
            )
        return
    if supplied:
        raise DomainError(
            code="WORKFLOW_TRANSITION_INVALID",
            module="m0",
            message="state baseline metadata requires its dedicated checkpoint",
            recoverable=True,
        )


def _validate_policy_transition(
    current: AssessmentRun,
    *,
    checkpoint: str,
    policy_id: str | None,
    adapter_id: str | None,
    adapter_version: str | None,
    artifact_sha256: str | None,
    feature_schema_version: str | None,
    action_space_version: str | None,
    gate_policy_version: str | None,
) -> None:
    required = (
        policy_id,
        adapter_id,
        adapter_version,
        feature_schema_version,
        action_space_version,
        gate_policy_version,
    )
    supplied = artifact_sha256 is not None or any(
        value is not None for value in required
    )
    if checkpoint == "policy_frozen":
        if current.policy_id is not None or any(
            value is None for value in required
        ):
            raise DomainError(
                code="WORKFLOW_RECOVERY_CONTEXT_MISSING",
                module="m0",
                message="assessment policy identity cannot be frozen safely",
                recoverable=True,
            )
        return
    if supplied:
        raise DomainError(
            code="WORKFLOW_TRANSITION_INVALID",
            module="m0",
            message="policy metadata requires its dedicated checkpoint",
            recoverable=True,
        )


def _require_live_lease(current: AssessmentRun, *, now: datetime) -> None:
    if (
        current.status != "running"
        or current.lease_until is None
        or current.lease_until <= now
    ):
        raise DomainError(
            code="WORKFLOW_LEASE_LOST",
            module="m0",
            message="assessment workflow lease is no longer active",
            recoverable=True,
        )


def _version_conflict(
    message: str = "assessment workflow row is stale or not owned",
) -> DomainError:
    return DomainError(
        code="WORKFLOW_VERSION_CONFLICT",
        module="m0",
        message=message,
        recoverable=True,
    )


def _submission_conflict(message: str) -> DomainError:
    return DomainError(
        code="ASSESSMENT_SUBMISSION_CONFLICT",
        module="m0",
        message=message,
        details={"operation": "submit"},
        recoverable=True,
    )


def _review_busy() -> DomainError:
    return DomainError(
        code="WORKFLOW_BUSY",
        module="m0",
        message="a prior teacher review is still in progress",
        details={"operation": "review"},
        recoverable=True,
    )


def _required_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field} is invalid")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    return None if value is None else _required_text(value, field=field)


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} is invalid")
    return value


def _optional_positive_int(value: object, *, field: str) -> int | None:
    return None if value is None else _positive_int(value, field=field)


def _optional_bool(value: object, *, field: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"{field} is invalid")
    return value


def _required_datetime(value: object, *, field: str) -> datetime:
    if type(value) is datetime:
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{field} is invalid") from None
    else:
        raise ValueError(f"{field} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} is invalid")
    return parsed


def _optional_datetime(value: object, *, field: str) -> datetime | None:
    return None if value is None else _required_datetime(value, field=field)


PostgreSQLM0Repository = PostgresM0Repository

__all__ = [
    "PostgresM0Repository",
    "PostgresM0RepositoryError",
    "PostgreSQLM0Repository",
]
