"""SQLite implementation of the M0 persistence boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.m0_outbox_repository import (
    SQLiteM0OutboxRepositoryMixin,
)
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
    validate_m0_schema,
)
from course_insight.modules.m0_platform.workflow import (
    AssessmentRun,
    advance_run,
    assert_assessment_run_replay,
    reconcile_legacy_assessment_run,
)


class SQLiteM0Repository(SQLiteM0OutboxRepositoryMixin):
    """Persist M0 records without leaking SQLite into the service layer."""

    def __init__(
        self,
        database_path: Path,
        *,
        outbox_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database_path = database_path
        self._outbox_clock = (
            (lambda: datetime.now(timezone.utc))
            if outbox_clock is None
            else outbox_clock
        )

    def initialize(self) -> None:
        """Create the database and apply all pending migrations."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def schema_is_current(self) -> bool:
        """Check the migration version and M0 storage structure."""

        connection = connect_sqlite(self._database_path)
        try:
            if current_schema_version(connection) != SCHEMA_VERSION:
                return False
            try:
                validate_m0_schema(connection)
            except RuntimeError:
                return False
            return True
        finally:
            connection.close()

    def insert_or_get_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """Insert one workflow row or return the authoritative replay row."""

        candidate = _isolated_run(run)
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = _workflow_row(connection, candidate.operation_id)
            if existing is not None:
                authoritative = _run_from_row(existing)
                reconciled = reconcile_legacy_assessment_run(
                    authoritative,
                    candidate,
                )
                if reconciled is not authoritative:
                    if authoritative.knowledge_bundle_id is None:
                        _adopt_workflow_dependencies(
                            connection,
                            expected=authoritative,
                            updated=reconciled,
                        )
                    else:
                        _adopt_workflow_policy(
                            connection,
                            expected=authoritative,
                            updated=reconciled,
                        )
                    authoritative = reconciled
                connection.execute("COMMIT")
                return authoritative
            conflict = (
                connection.execute(
                    """
                    SELECT operation_id
                    FROM m0_assessment_runs
                    WHERE operation = 'submit' AND paper_id = ?
                    """,
                    (candidate.paper_id,),
                ).fetchone()
                if candidate.operation == "submit"
                else None
            )
            if conflict is not None:
                raise _submission_conflict(candidate.operation)
            review_conflict = (
                connection.execute(
                    """
                    SELECT operation_id
                    FROM m0_assessment_runs
                    WHERE operation = 'review'
                      AND status <> 'completed'
                      AND paper_id = ?
                    ORDER BY created_at, operation_id
                    LIMIT 1
                    """,
                    (candidate.paper_id,),
                ).fetchone()
                if candidate.operation == "review"
                else None
            )
            if review_conflict is not None:
                raise _review_busy()
            try:
                _insert_workflow_row(connection, candidate)
            except sqlite3.IntegrityError:
                if candidate.operation == "review":
                    review_conflict = connection.execute(
                        """
                        SELECT operation_id
                        FROM m0_assessment_runs
                        WHERE operation = 'review'
                          AND status <> 'completed'
                          AND paper_id = ?
                        ORDER BY created_at, operation_id
                        LIMIT 1
                        """,
                        (candidate.paper_id,),
                    ).fetchone()
                    if review_conflict is not None:
                        raise _review_busy() from None
                raise
            stored = _workflow_row(connection, candidate.operation_id)
            if stored is None:
                raise RuntimeError("assessment workflow insert produced no row")
            authoritative = _run_from_row(stored)
            _assert_same_run(authoritative, candidate)
            connection.execute("COMMIT")
            return authoritative
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def adopt_legacy_assessment_run(
        self,
        run: AssessmentRun,
    ) -> AssessmentRun:
        """CAS-fill dependencies on one exact, wholly legacy workflow row."""

        candidate = _isolated_run(run)
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, candidate.operation_id)
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
            if reconciled is not current:
                if current.knowledge_bundle_id is None:
                    _adopt_workflow_dependencies(
                        connection,
                        expected=current,
                        updated=reconciled,
                    )
                else:
                    _adopt_workflow_policy(
                        connection,
                        expected=current,
                        updated=reconciled,
                    )
                current = reconciled
            connection.execute("COMMIT")
            return current
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_assessment_run(
        self,
        operation_id: str,
    ) -> AssessmentRun | None:
        """Load one exact workflow row."""

        connection = connect_sqlite(self._database_path)
        try:
            row = _workflow_row(connection, operation_id)
            return None if row is None else _run_from_row(row)
        finally:
            connection.close()

    def list_assessment_runs(self) -> tuple[AssessmentRun, ...]:
        """Load every workflow row for offline legacy inventory."""

        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                f"""
                SELECT {_WORKFLOW_COLUMNS}
                FROM m0_assessment_runs
                ORDER BY operation_id
                """
            ).fetchall()
            return tuple(_run_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_assessment_run_by_paper(
        self,
        paper_id: str,
        *,
        operation: str | None = None,
        status: str | None = None,
    ) -> AssessmentRun | None:
        """Load the latest workflow row for a paper and optional operation."""

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
        """Load the latest workflow row for an attempt and optional operation."""

        return self._get_assessment_run_by_reference(
            "attempt_id",
            attempt_id,
            operation=operation,
            status=status,
        )

    def _get_assessment_run_by_reference(
        self,
        column: str,
        value: str,
        *,
        operation: str | None,
        status: str | None,
    ) -> AssessmentRun | None:
        connection = connect_sqlite(self._database_path)
        try:
            where_operation = "" if operation is None else "AND operation = ?"
            where_status = "" if status is None else "AND status = ?"
            parameters = tuple(
                item
                for item in (value, operation, status)
                if item is not None
            )
            row = connection.execute(
                f"""
                SELECT {_WORKFLOW_COLUMNS}
                FROM m0_assessment_runs
                WHERE {column} = ? {where_operation} {where_status}
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return None if row is None else _run_from_row(row)
        finally:
            connection.close()

    def claim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun | None:
        """Claim one runnable workflow row."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                connection.execute("COMMIT")
                return None
            current = _run_from_row(row)
            if current.status not in {"pending", "failed"}:
                connection.execute("COMMIT")
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
            connection.execute("COMMIT")
            return claimed
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                connection.execute("COMMIT")
                return False
            current = _run_from_row(row)
            if (
                current.version != expected_version
                or current.status != "running"
                or current.locked_by != worker_id
                or current.lease_until is None
                or current.lease_until <= now
            ):
                connection.execute("COMMIT")
                return False
            cursor = connection.execute(
                """
                UPDATE m0_assessment_runs
                SET lease_until = ?, updated_at = ?
                WHERE operation_id = ?
                  AND version = ?
                  AND status = 'running'
                  AND locked_by = ?
                  AND lease_until = ?
                """,
                (
                    lease_until.isoformat(),
                    now.isoformat(),
                    operation_id,
                    expected_version,
                    worker_id,
                    current.lease_until.isoformat(),
                ),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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
        """Advance one claimed workflow row by one legal checkpoint."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
            if current.version != expected_version or current.locked_by != worker_id:
                raise DomainError(
                    code="WORKFLOW_VERSION_CONFLICT",
                    module="m0",
                    message="assessment workflow row is stale or not owned",
                    recoverable=True,
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
            _update_workflow_row(connection, expected=current, updated=updated)
            connection.execute("COMMIT")
            return updated
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def complete_assessment_run(
        self,
        operation_id: str,
        *,
        expected_version: int,
        worker_id: str,
        now: datetime,
    ) -> AssessmentRun:
        """Complete the next legal terminal transition and release its lease."""

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
        """Keep the last checkpoint, record a safe code, and release its lease."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
            if (
                current.version != expected_version
                or current.locked_by != worker_id
                or current.status != "running"
            ):
                raise DomainError(
                    code="WORKFLOW_VERSION_CONFLICT",
                    module="m0",
                    message="assessment workflow row is stale or not owned",
                    recoverable=True,
                )
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
            _update_workflow_row(connection, expected=current, updated=failed)
            connection.execute("COMMIT")
            return failed
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
            if (
                current.version != expected_version
                or current.locked_by != worker_id
                or current.status != "running"
                or status not in WAITING_WORKFLOW_STATUSES
            ):
                raise DomainError(
                    code="WORKFLOW_VERSION_CONFLICT",
                    module="m0",
                    message="assessment workflow row is stale or not owned",
                    recoverable=True,
                )
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
            _update_workflow_row(connection, expected=current, updated=parked)
            connection.execute("COMMIT")
            return parked
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
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
            _update_workflow_row(connection, expected=current, updated=resumed)
            connection.execute("COMMIT")
            return resumed
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

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

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
            if (
                current.version != expected_version
                or current.locked_by != worker_id
                or current.status != "running"
            ):
                raise DomainError(
                    code="WORKFLOW_VERSION_CONFLICT",
                    module="m0",
                    message="assessment workflow row is stale or not owned",
                    recoverable=True,
                )
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
            _update_workflow_row(connection, expected=current, updated=finished)
            connection.execute("COMMIT")
            return finished
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def reclaim_assessment_run(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_until: datetime,
    ) -> AssessmentRun:
        """Take over an expired workflow lease without altering its checkpoint."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _workflow_row(connection, operation_id)
            if row is None:
                raise DomainError(
                    code="WORKFLOW_NOT_FOUND",
                    module="m0",
                    message="assessment workflow row does not exist",
                    recoverable=True,
                )
            current = _run_from_row(row)
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
            _update_workflow_row(connection, expected=current, updated=reclaimed)
            connection.execute("COMMIT")
            return reclaimed
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


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
    connection,
    operation_id: str,
):
    return connection.execute(
        f"""
        SELECT {_WORKFLOW_COLUMNS}
        FROM m0_assessment_runs
        WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()


def _isolated_run(run: AssessmentRun) -> AssessmentRun:
    return AssessmentRun(
        operation_id=run.operation_id,
        operation=run.operation,
        request_checksum=run.request_checksum,
        course_id=run.course_id,
        class_id=run.class_id,
        learner_id=run.learner_id,
        session_id=run.session_id,
        task_id=run.task_id,
        paper_id=run.paper_id,
        attempt_id=run.attempt_id,
        feedback_id=run.feedback_id,
        report_id=run.report_id,
        scoring_result_checksum=run.scoring_result_checksum,
        target_audit_id=run.target_audit_id,
        target_audit_version=run.target_audit_version,
        state_version=run.state_version,
        knowledge_bundle_id=run.knowledge_bundle_id,
        knowledge_bundle_version=run.knowledge_bundle_version,
        knowledge_bundle_checksum=run.knowledge_bundle_checksum,
        course_package_id=run.course_package_id,
        evidence_index_id=run.evidence_index_id,
        evidence_index_version=run.evidence_index_version,
        evidence_index_checksum=run.evidence_index_checksum,
        state_policy_checksum=run.state_policy_checksum,
        teacher_policy_checksum=run.teacher_policy_checksum,
        class_roster_size=run.class_roster_size,
        class_roster_checksum=run.class_roster_checksum,
        class_roster_captured_at=run.class_roster_captured_at,
        previous_state_frozen=run.previous_state_frozen,
        previous_learner_snapshot_id=run.previous_learner_snapshot_id,
        previous_learner_state_version=run.previous_learner_state_version,
        previous_class_snapshot_id=run.previous_class_snapshot_id,
        previous_class_state_version=run.previous_class_state_version,
        policy_id=run.policy_id,
        adapter_id=run.adapter_id,
        adapter_version=run.adapter_version,
        artifact_sha256=run.artifact_sha256,
        feature_schema_version=run.feature_schema_version,
        action_space_version=run.action_space_version,
        gate_policy_version=run.gate_policy_version,
        checkpoint=run.checkpoint,
        status=run.status,
        version=run.version,
        locked_by=run.locked_by,
        lease_until=run.lease_until,
        error_code=run.error_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _insert_workflow_row(connection, run: AssessmentRun) -> None:
    connection.execute(
        """
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
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
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
            (
                None
                if run.class_roster_captured_at is None
                else run.class_roster_captured_at.isoformat()
            ),
            (
                None
                if run.previous_state_frozen is None
                else int(run.previous_state_frozen)
            ),
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
            None if run.lease_until is None else run.lease_until.isoformat(),
            run.error_code,
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
        ),
    )


def _update_workflow_row(
    connection,
    *,
    expected: AssessmentRun,
    updated: AssessmentRun,
) -> None:
    cursor = connection.execute(
        """
        UPDATE m0_assessment_runs
        SET checkpoint = ?,
            status = ?,
            version = ?,
            locked_by = ?,
            lease_until = ?,
            feedback_id = ?,
            report_id = ?,
            scoring_result_checksum = ?,
            state_version = ?,
            previous_state_frozen = ?,
            previous_learner_snapshot_id = ?,
            previous_learner_state_version = ?,
            previous_class_snapshot_id = ?,
            previous_class_state_version = ?,
            policy_id = ?,
            adapter_id = ?,
            adapter_version = ?,
            artifact_sha256 = ?,
            feature_schema_version = ?,
            action_space_version = ?,
            gate_policy_version = ?,
            error_code = ?,
            updated_at = ?
        WHERE operation_id = ? AND version = ?
        """,
        (
            updated.checkpoint,
            updated.status,
            updated.version,
            updated.locked_by,
            None if updated.lease_until is None else updated.lease_until.isoformat(),
            updated.feedback_id,
            updated.report_id,
            updated.scoring_result_checksum,
            updated.state_version,
            (
                None
                if updated.previous_state_frozen is None
                else int(updated.previous_state_frozen)
            ),
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
            updated.updated_at.isoformat(),
            expected.operation_id,
            expected.version,
        ),
    )
    if cursor.rowcount != 1:
        raise DomainError(
            code="WORKFLOW_VERSION_CONFLICT",
            module="m0",
            message="assessment workflow row changed concurrently",
            recoverable=True,
        )


def _adopt_workflow_dependencies(
    connection,
    *,
    expected: AssessmentRun,
    updated: AssessmentRun,
) -> None:
    cursor = connection.execute(
        """
        UPDATE m0_assessment_runs
        SET knowledge_bundle_id = ?,
            knowledge_bundle_version = ?,
            knowledge_bundle_checksum = ?,
            course_package_id = ?,
            evidence_index_id = ?,
            evidence_index_version = ?,
            evidence_index_checksum = ?,
            state_policy_checksum = ?,
            teacher_policy_checksum = ?,
            class_roster_size = ?,
            class_roster_checksum = ?,
            class_roster_captured_at = ?,
            previous_state_frozen = ?,
            policy_id = ?,
            adapter_id = ?,
            adapter_version = ?,
            artifact_sha256 = ?,
            feature_schema_version = ?,
            action_space_version = ?,
            gate_policy_version = ?,
            version = ?,
            updated_at = ?
        WHERE operation_id = ?
          AND version = ?
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
            (
                None
                if updated.class_roster_captured_at is None
                else updated.class_roster_captured_at.isoformat()
            ),
            (
                None
                if updated.previous_state_frozen is None
                else int(updated.previous_state_frozen)
            ),
            updated.policy_id,
            updated.adapter_id,
            updated.adapter_version,
            updated.artifact_sha256,
            updated.feature_schema_version,
            updated.action_space_version,
            updated.gate_policy_version,
            updated.version,
            updated.updated_at.isoformat(),
            expected.operation_id,
            expected.version,
        ),
    )
    if cursor.rowcount != 1:
        raise DomainError(
            code="WORKFLOW_VERSION_CONFLICT",
            module="m0",
            message="assessment workflow row changed concurrently",
            recoverable=True,
        )


def _adopt_workflow_policy(
    connection,
    *,
    expected: AssessmentRun,
    updated: AssessmentRun,
) -> None:
    cursor = connection.execute(
        """
        UPDATE m0_assessment_runs
        SET policy_id = ?,
            adapter_id = ?,
            adapter_version = ?,
            artifact_sha256 = ?,
            feature_schema_version = ?,
            action_space_version = ?,
            gate_policy_version = ?,
            version = ?,
            updated_at = ?
        WHERE operation_id = ?
          AND version = ?
          AND policy_id IS NULL
          AND adapter_id IS NULL
          AND adapter_version IS NULL
          AND artifact_sha256 IS NULL
          AND feature_schema_version IS NULL
          AND action_space_version IS NULL
          AND gate_policy_version IS NULL
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
            updated.updated_at.isoformat(),
            expected.operation_id,
            expected.version,
        ),
    )
    if cursor.rowcount != 1:
        raise DomainError(
            code="WORKFLOW_VERSION_CONFLICT",
            module="m0",
            message="assessment workflow row changed concurrently",
            recoverable=True,
        )


def _run_from_row(row) -> AssessmentRun:
    return AssessmentRun(
        operation_id=str(row["operation_id"]),
        operation=str(row["operation"]),
        request_checksum=str(row["request_checksum"]),
        course_id=str(row["course_id"]),
        class_id=str(row["class_id"]),
        learner_id=str(row["learner_id"]),
        session_id=str(row["session_id"]),
        task_id=str(row["task_id"]),
        paper_id=str(row["paper_id"]),
        attempt_id=None if row["attempt_id"] is None else str(row["attempt_id"]),
        feedback_id=(
            None if row["feedback_id"] is None else str(row["feedback_id"])
        ),
        report_id=None if row["report_id"] is None else str(row["report_id"]),
        scoring_result_checksum=(
            None
            if row["scoring_result_checksum"] is None
            else str(row["scoring_result_checksum"])
        ),
        target_audit_id=(
            None
            if row["target_audit_id"] is None
            else str(row["target_audit_id"])
        ),
        target_audit_version=(
            None
            if row["target_audit_version"] is None
            else int(row["target_audit_version"])
        ),
        state_version=(
            None if row["state_version"] is None else int(row["state_version"])
        ),
        knowledge_bundle_id=(
            None
            if row["knowledge_bundle_id"] is None
            else str(row["knowledge_bundle_id"])
        ),
        knowledge_bundle_version=(
            None
            if row["knowledge_bundle_version"] is None
            else str(row["knowledge_bundle_version"])
        ),
        knowledge_bundle_checksum=(
            None
            if row["knowledge_bundle_checksum"] is None
            else str(row["knowledge_bundle_checksum"])
        ),
        course_package_id=(
            None
            if row["course_package_id"] is None
            else str(row["course_package_id"])
        ),
        evidence_index_id=(
            None
            if row["evidence_index_id"] is None
            else str(row["evidence_index_id"])
        ),
        evidence_index_version=(
            None
            if row["evidence_index_version"] is None
            else str(row["evidence_index_version"])
        ),
        evidence_index_checksum=(
            None
            if row["evidence_index_checksum"] is None
            else str(row["evidence_index_checksum"])
        ),
        state_policy_checksum=(
            None
            if row["state_policy_checksum"] is None
            else str(row["state_policy_checksum"])
        ),
        teacher_policy_checksum=(
            None
            if row["teacher_policy_checksum"] is None
            else str(row["teacher_policy_checksum"])
        ),
        class_roster_size=(
            None
            if row["class_roster_size"] is None
            else int(row["class_roster_size"])
        ),
        class_roster_checksum=(
            None
            if row["class_roster_checksum"] is None
            else str(row["class_roster_checksum"])
        ),
        class_roster_captured_at=(
            None
            if row["class_roster_captured_at"] is None
            else datetime.fromisoformat(str(row["class_roster_captured_at"]))
        ),
        previous_state_frozen=(
            _optional_bool(row["previous_state_frozen"])
        ),
        previous_learner_snapshot_id=(
            None
            if row["previous_learner_snapshot_id"] is None
            else str(row["previous_learner_snapshot_id"])
        ),
        previous_learner_state_version=(
            None
            if row["previous_learner_state_version"] is None
            else int(row["previous_learner_state_version"])
        ),
        previous_class_snapshot_id=(
            None
            if row["previous_class_snapshot_id"] is None
            else str(row["previous_class_snapshot_id"])
        ),
        previous_class_state_version=(
            None
            if row["previous_class_state_version"] is None
            else int(row["previous_class_state_version"])
        ),
        policy_id=(
            None if row["policy_id"] is None else str(row["policy_id"])
        ),
        adapter_id=(
            None if row["adapter_id"] is None else str(row["adapter_id"])
        ),
        adapter_version=(
            None
            if row["adapter_version"] is None
            else str(row["adapter_version"])
        ),
        artifact_sha256=(
            None
            if row["artifact_sha256"] is None
            else str(row["artifact_sha256"])
        ),
        feature_schema_version=(
            None
            if row["feature_schema_version"] is None
            else str(row["feature_schema_version"])
        ),
        action_space_version=(
            None
            if row["action_space_version"] is None
            else str(row["action_space_version"])
        ),
        gate_policy_version=(
            None
            if row["gate_policy_version"] is None
            else str(row["gate_policy_version"])
        ),
        checkpoint=str(row["checkpoint"]),
        status=str(row["status"]),
        version=int(row["version"]),
        locked_by=None if row["locked_by"] is None else str(row["locked_by"]),
        lease_until=(
            None
            if row["lease_until"] is None
            else datetime.fromisoformat(str(row["lease_until"]))
        ),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _assert_same_run(left: AssessmentRun, right: AssessmentRun) -> None:
    assert_assessment_run_replay(left, right)


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is not int or value not in {0, 1}:
        raise ValueError("previous_state_frozen is invalid")
    return bool(value)


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


def _submission_conflict(operation: str) -> DomainError:
    return DomainError(
        code="ASSESSMENT_SUBMISSION_CONFLICT",
        module="m0",
        message="assessment workflow already exists for this paper",
        details={"operation": operation},
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
