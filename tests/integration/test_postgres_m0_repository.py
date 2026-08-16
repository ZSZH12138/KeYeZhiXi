"""PostgreSQL M0 SQL/protocol tests plus an opt-in real database smoke test.

The real test reads only ``COURSE_INSIGHT_TEST_DATABASE_URL`` and skips when
it is absent.  That URL must name a disposable test database because the
fixture rebuilds its application schema; production ``DATABASE_URL`` is never
consulted.
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from inspect import signature
from threading import Barrier, Lock, Thread

import psycopg
import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
    PostgresM0RepositoryError,
)
from course_insight.modules.m0_platform.repository import M0Repository
from course_insight.modules.m0_platform.workflow import AssessmentRun
from tests.integration._postgres_live import require_live_test_database_url


NOW = datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)
LEASE = NOW + timedelta(seconds=30)
_UNSET = object()


@dataclass(frozen=True)
class _Step:
    contains: tuple[str, ...]
    one: object = _UNSET
    all_rows: object = _UNSET
    rowcount: int = -1
    error: BaseException | None = None
    check: Callable[[tuple[object, ...]], None] | None = None


class _Cursor:
    def __init__(self, step: _Step) -> None:
        self._step = step
        self.rowcount = step.rowcount

    def fetchone(self) -> object:
        if self._step.one is _UNSET:
            raise AssertionError("unexpected fetchone()")
        return self._step.one

    def fetchall(self) -> object:
        if self._step.all_rows is _UNSET:
            raise AssertionError("unexpected fetchall()")
        return self._step.all_rows


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection

    def __enter__(self) -> None:
        assert self._connection.depth == 0
        self._connection.depth = 1
        self._connection.outcomes.append("begin")

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> bool:
        del error, traceback
        self._connection.outcomes.append(
            "rollback" if error_type is not None else "commit"
        )
        self._connection.depth = 0
        return False


class _Connection:
    def __init__(self, steps: list[_Step]) -> None:
        self.steps = deque(steps)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.outcomes: list[str] = []
        self.depth = 0

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        assert self.depth == 1, "SQL must execute inside a bounded transaction"
        if not self.steps:
            raise AssertionError(f"unexpected SQL: {statement}")
        step = self.steps.popleft()
        normalized = " ".join(statement.split())
        for fragment in step.contains:
            assert fragment in normalized, normalized
        values = tuple(parameters)
        if step.check is not None:
            step.check(values)
        self.calls.append((normalized, values))
        if step.error is not None:
            raise step.error
        return _Cursor(step)


class _Pool:
    def __init__(self, *connections: _Connection) -> None:
        self.connections = deque(connections)
        self.used: list[_Connection] = []
        self.checked_out = 0

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        if not self.connections:
            raise AssertionError("unexpected pool checkout")
        connection = self.connections.popleft()
        self.used.append(connection)
        self.checked_out += 1
        try:
            yield connection
        finally:
            self.checked_out -= 1

    def assert_consumed(self) -> None:
        assert not self.connections
        for connection in self.used:
            assert not connection.steps
            assert connection.depth == 0


def _event(
    event_id: str = "事件/学习 🧪",
    *,
    score: int = 7,
) -> LearningEvent:
    return LearningEvent(
        event_id=event_id,
        event_type="assessment_completed",
        course_id="course-1",
        class_id="class-1",
        learner_id="learner-1",
        attempt_id="attempt-1",
        payload={"score": score, "labels": ["掌握", "复习"]},
        occurred_at=NOW,
    )


def _run(
    *,
    operation_id: str = "submit-1",
    paper_id: str = "paper-1",
    operation: str = "submit",
    request_checksum: str = "request-checksum",
    checkpoint: str = "pending",
    status: str = "pending",
    version: int = 1,
    scoring_result_checksum: str | None = None,
    state_version: int | None = None,
) -> AssessmentRun:
    return AssessmentRun(
        operation_id=operation_id,
        operation=operation,
        request_checksum=request_checksum,
        course_id="course-1",
        class_id="class-1",
        learner_id="learner-1",
        session_id="session-1",
        task_id="task-1",
        paper_id=paper_id,
        attempt_id="attempt-1",
        feedback_id=None,
        report_id=("report-1" if operation == "review" and status == "completed" else None),
        checkpoint=checkpoint,
        status=status,
        version=version,
        locked_by=None,
        lease_until=None,
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
        scoring_result_checksum=scoring_result_checksum,
        target_audit_id=("audit-1" if operation == "review" else None),
        target_audit_version=(1 if operation == "review" else None),
        state_version=state_version,
    )


def _row(run: AssessmentRun) -> dict[str, object]:
    return {
        field: getattr(run, field)
        for field in run.__dataclass_fields__
    }


def _outbox_row(event: LearningEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "record": dumps_json(event.to_dict()),
        "status": "processing",
        "attempt_count": 1,
        "version": 2,
        "available_at": NOW,
        "locked_by": "worker-1",
        "locked_at": NOW,
        "lease_until": LEASE,
        "last_error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
    }


def test_repository_structurally_implements_complete_m0_protocol() -> None:
    repository = PostgresM0Repository(_Pool())
    protocol_methods = {
        name
        for name, value in vars(M0Repository).items()
        if callable(value) and not name.startswith("_")
    }
    assert protocol_methods <= set(dir(repository))
    for method in protocol_methods:
        assert (
            signature(getattr(type(repository), method)).parameters
            == signature(getattr(M0Repository, method)).parameters
        )


def test_append_events_is_atomic_canonical_unicode_and_disjoint() -> None:
    event = _event()

    def check_event(parameters: tuple[object, ...]) -> None:
        assert parameters[:4] == (
            event.event_id,
            event.idempotency_key(),
            event.event_type,
            NOW,
        )
        assert parameters[4] == dumps_json(event.payload)

    def check_outbox(parameters: tuple[object, ...]) -> None:
        assert parameters[0] == event.event_id
        assert parameters[1] == dumps_json(event.to_dict())

    connection = _Connection(
        [
            _Step(
                ("INSERT INTO m0_learning_events", "ON CONFLICT", "RETURNING"),
                one={"event_id": event.event_id},
                rowcount=1,
                check=check_event,
            ),
            _Step(
                ("INSERT INTO m0_event_outbox", "'pending'", "VALUES"),
                rowcount=1,
                check=check_outbox,
            ),
        ]
    )
    pool = _Pool(connection)
    result = PostgresM0Repository(pool).append_events([event, event])
    assert result == ((event.event_id,), ())
    assert connection.outcomes == ["begin", "commit"]
    pool.assert_consumed()

def test_append_events_reports_stored_duplicate_without_narrowing_event_id() -> None:
    event = _event("空 格/emoji 🚀/路径")
    connection = _Connection(
        [
            _Step(
                ("INSERT INTO m0_learning_events", "ON CONFLICT", "RETURNING"),
                one=None,
                rowcount=0,
            )
        ]
    )
    result = PostgresM0Repository(_Pool(connection)).append_events([event])
    assert result == ((), (event.event_id,))
    assert connection.outcomes == ["begin", "commit"]

def test_append_events_rolls_back_both_rows_and_redacts_driver_failure() -> None:
    event = _event()
    connection = _Connection(
        [
            _Step(
                ("INSERT INTO m0_learning_events",),
                one={"event_id": event.event_id},
                rowcount=1,
            ),
            _Step(
                ("INSERT INTO m0_event_outbox",),
                error=psycopg.DataError(
                    "postgresql://private-user:secret@db.internal/course"
                ),
            ),
        ]
    )
    with pytest.raises(PostgresM0RepositoryError) as captured:
        PostgresM0Repository(_Pool(connection)).append_events([event])
    assert connection.outcomes == ["begin", "rollback"]
    assert "secret" not in str(captured.value)
    assert "postgresql://" not in str(captured.value)

def test_claim_outbox_uses_skip_locked_and_reclaims_expired_lease() -> None:
    event = _event()
    row = _outbox_row(event)
    connection = _Connection(
        [
            _Step(
                (
                    "status = 'pending'",
                    "status = 'processing'",
                    "lease_until <= %s",
                    "FOR UPDATE SKIP LOCKED",
                    "LIMIT %s",
                ),
                all_rows=[{"event_id": event.event_id, "version": 1}],
            ),
            _Step(
                (
                    "UPDATE m0_event_outbox",
                    "attempt_count = attempt_count + 1",
                    "version = version + 1",
                    "RETURNING",
                ),
                one=row,
                rowcount=1,
            ),
        ]
    )

    records = PostgresM0Repository(_Pool(connection)).claim_outbox_batch(
        "worker-1",
        now=NOW,
        lease_until=LEASE,
        batch_size=10,
    )

    assert len(records) == 1
    assert records[0].event_id == event.event_id
    assert records[0].attempt_count == 1
    assert records[0].version == 2
    assert connection.outcomes == ["begin", "commit"]

def test_claim_outbox_rolls_back_on_noncanonical_persisted_record() -> None:
    event = _event()
    corrupt = {
        **_outbox_row(event),
        "record": dumps_json(event.to_dict()).replace('":', '": ', 1),
    }
    connection = _Connection(
        [
            _Step(
                ("FOR UPDATE SKIP LOCKED",),
                all_rows=[{"event_id": event.event_id, "version": 1}],
            ),
            _Step(
                ("UPDATE m0_event_outbox", "RETURNING"),
                one=corrupt,
                rowcount=1,
            ),
        ]
    )

    with pytest.raises(ValueError, match="canonical JSON"):
        PostgresM0Repository(_Pool(connection)).claim_outbox_batch(
            "worker-1",
            now=NOW,
            lease_until=LEASE,
            batch_size=1,
        )

    assert connection.outcomes == ["begin", "rollback"]


def test_outbox_ack_fail_and_renew_are_worker_version_cas() -> None:
    event_id = _event().event_id
    ack = _Connection(
        [
            _Step(
                (
                    "DELETE FROM m0_event_outbox",
                    "status = 'processing'",
                    "locked_by = %s",
                    "version = %s",
                    "RETURNING event_id",
                ),
                one={"event_id": event_id},
                rowcount=1,
            )
        ]
    )
    failed = _Connection(
        [
            _Step(
                (
                    "UPDATE m0_event_outbox",
                    "locked_by = %s",
                    "version = %s",
                    "RETURNING event_id",
                ),
                one=None,
                rowcount=0,
            )
        ]
    )
    renewed = _Connection(
        [
            _Step(
                (
                    "UPDATE m0_event_outbox",
                    "lease_until > %s",
                    "locked_by = %s",
                    "version = %s",
                    "RETURNING event_id",
                ),
                one={"event_id": event_id},
                rowcount=1,
            )
        ]
    )
    pool = _Pool(ack, failed, renewed)
    repository = PostgresM0Repository(pool)

    assert repository.mark_outbox_delivered(
        "worker-1", [(event_id, 2)]
    ) == (event_id,)
    assert repository.mark_outbox_failed(
        "worker-1",
        event_id,
        expected_version=2,
        now=NOW,
        next_attempt_at=NOW,
        error_code="IO_FAILED",
        dead=False,
    ) is False
    assert repository.renew_outbox_leases(
        "worker-1",
        [(event_id, 2)],
        now=NOW,
        lease_until=LEASE,
    ) == (event_id,)
    pool.assert_consumed()


def test_compatibility_callback_runs_outside_database_transactions() -> None:
    event = _event()
    claim = _Connection(
        [
            _Step(
                ("FOR UPDATE SKIP LOCKED",),
                all_rows=[{"event_id": event.event_id, "version": 1}],
            ),
            _Step(
                ("UPDATE m0_event_outbox",),
                one=_outbox_row(event),
                rowcount=1,
            ),
        ]
    )
    ack = _Connection(
        [
            _Step(
                ("DELETE FROM m0_event_outbox",),
                one={"event_id": event.event_id},
                rowcount=1,
            )
        ]
    )
    empty = _Connection(
        [_Step(("FOR UPDATE SKIP LOCKED",), all_rows=[])]
    )
    pool = _Pool(claim, ack, empty)
    delivered: list[str] = []

    def callback(event_id: str, record: str) -> None:
        assert pool.checked_out == 0
        assert all(connection.depth == 0 for connection in pool.used)
        assert record == dumps_json(event.to_dict())
        delivered.append(event_id)

    PostgresM0Repository(pool).deliver_outbox_records(callback)

    assert delivered == [event.event_id]
    pool.assert_consumed()


def test_insert_or_get_workflow_replays_or_conflicts_by_authority() -> None:
    candidate = _run()
    insert = _Connection(
        [
            _Step(
                (
                    "INSERT INTO m0_assessment_runs",
                    "ON CONFLICT DO NOTHING",
                    "RETURNING",
                ),
                one=_row(candidate),
                rowcount=1,
            )
        ]
    )
    replay = _Connection(
        [
            _Step(
                ("INSERT INTO m0_assessment_runs", "ON CONFLICT DO NOTHING"),
                one=None,
                rowcount=0,
            ),
            _Step(
                ("WHERE operation_id = %s",),
                one=_row(candidate),
            ),
        ]
    )
    conflicting = replace(candidate, request_checksum="different")
    conflict = _Connection(
        [
            _Step(
                ("INSERT INTO m0_assessment_runs",),
                one=None,
                rowcount=0,
            ),
            _Step(
                ("WHERE operation_id = %s",),
                one=_row(candidate),
            ),
        ]
    )
    pool = _Pool(insert, replay, conflict)
    repository = PostgresM0Repository(pool)

    assert repository.insert_or_get_assessment_run(candidate) == candidate
    assert repository.insert_or_get_assessment_run(candidate) == candidate
    with pytest.raises(DomainError) as captured:
        repository.insert_or_get_assessment_run(conflicting)

    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"
    assert conflict.outcomes == ["begin", "rollback"]
    pool.assert_consumed()


def test_insert_or_get_review_reports_busy_when_other_nonterminal_review_exists() -> None:
    candidate = _run(
        operation_id="review-2",
        operation="review",
        request_checksum="audit-1:v2",
    )
    existing = _run(
        operation_id="review-1",
        operation="review",
        request_checksum="audit-1:v1",
    )
    conflict = _Connection(
        [
            _Step(
                ("INSERT INTO m0_assessment_runs", "ON CONFLICT DO NOTHING"),
                one=None,
                rowcount=0,
            ),
            _Step(
                ("WHERE operation_id = %s",),
                one=None,
            ),
            _Step(
                (
                    "WHERE operation = 'review'",
                    "status <> 'completed'",
                    "paper_id = %s",
                ),
                one=_row(existing),
            ),
        ]
    )

    with pytest.raises(DomainError) as captured:
        PostgresM0Repository(_Pool(conflict)).insert_or_get_assessment_run(
            candidate
        )

    assert captured.value.code == "WORKFLOW_BUSY"
    assert conflict.outcomes == ["begin", "rollback"]


def test_workflow_claim_advance_and_cas_reuse_domain_transitions() -> None:
    pending = _run()
    claim_connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_row(pending),
            ),
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "version = %s",
                    "locked_by IS NOT DISTINCT FROM %s",
                ),
                rowcount=1,
            ),
        ]
    )
    claimed = replace(
        pending,
        checkpoint="claimed",
        status="running",
        version=2,
        locked_by="workflow-worker",
        lease_until=LEASE,
        updated_at=NOW,
    )
    illegal = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_row(claimed),
            )
        ]
    )
    stale = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_row(claimed),
            )
        ]
    )
    pool = _Pool(claim_connection, illegal, stale)
    repository = PostgresM0Repository(pool)

    assert repository.claim_assessment_run(
        pending.operation_id,
        worker_id="workflow-worker",
        now=NOW,
        lease_until=LEASE,
    ) == claimed
    with pytest.raises(DomainError) as transition_error:
        repository.advance_assessment_run(
            pending.operation_id,
            expected_version=2,
            checkpoint="events_appended",
            worker_id="workflow-worker",
            now=NOW,
        )
    with pytest.raises(DomainError) as stale_error:
        repository.advance_assessment_run(
            pending.operation_id,
            expected_version=99,
            checkpoint="scoring_saved",
            worker_id="workflow-worker",
            now=NOW,
        )

    assert transition_error.value.code == "WORKFLOW_TRANSITION_INVALID"
    assert stale_error.value.code == "WORKFLOW_VERSION_CONFLICT"
    assert illegal.outcomes == ["begin", "rollback"]
    assert stale.outcomes == ["begin", "rollback"]
    pool.assert_consumed()


def test_workflow_reclaim_fail_and_complete_preserve_cas_ownership() -> None:
    running = replace(
        _run(),
        checkpoint="claimed",
        status="running",
        version=2,
        locked_by="old-worker",
        lease_until=NOW - timedelta(seconds=1),
    )
    reclaimed = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_row(running),
            ),
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "version = %s",
                    "locked_by IS NOT DISTINCT FROM %s",
                ),
                rowcount=1,
            ),
        ]
    )
    failed_run = replace(
        running,
        locked_by="new-worker",
        lease_until=LEASE,
        version=3,
        updated_at=NOW,
    )
    failed = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_row(failed_run),
            ),
            _Step(("UPDATE m0_assessment_runs",), rowcount=1),
        ]
    )
    precomplete = replace(
        failed_run,
        checkpoint="analytics_saved",
        version=7,
        feedback_id="feedback-1",
        report_id="report-1",
        scoring_result_checksum="a" * 64,
        state_version=2,
    )
    complete = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_row(precomplete),
            ),
            _Step(("UPDATE m0_assessment_runs",), rowcount=1),
        ]
    )
    pool = _Pool(reclaimed, failed, complete)
    repository = PostgresM0Repository(pool)

    taken = repository.reclaim_assessment_run(
        running.operation_id,
        worker_id="new-worker",
        now=NOW,
        lease_until=LEASE,
    )
    assert taken.version == 3
    assert taken.checkpoint == "claimed"
    assert repository.fail_assessment_run(
        running.operation_id,
        expected_version=3,
        worker_id="new-worker",
        error_code="SCORING_FAILED",
        now=NOW,
    ).status == "failed"
    terminal = repository.complete_assessment_run(
        running.operation_id,
        expected_version=7,
        worker_id="new-worker",
        now=NOW,
    )
    assert terminal.status == "completed"
    assert terminal.locked_by is None
    pool.assert_consumed()


def test_reference_lookup_is_parameterized_and_returns_domain_value() -> None:
    run = _run()
    hostile = "paper' OR TRUE --"

    def check(parameters: tuple[object, ...]) -> None:
        assert parameters == (hostile, "submit", "pending")

    connection = _Connection(
        [
            _Step(
                (
                    "WHERE paper_id = %s",
                    "operation = %s",
                    "status = %s",
                    "ORDER BY updated_at DESC",
                ),
                one=_row(run),
                check=check,
            )
        ]
    )

    restored = PostgresM0Repository(_Pool(connection)).get_assessment_run_by_paper(
        hostile,
        operation="submit",
        status="pending",
    )

    assert restored == run
    assert hostile not in connection.calls[0][0]


@pytest.fixture
def live_repository() -> Iterator[PostgresM0Repository]:
    """Create an M0 adapter only for an explicitly configured disposable DB."""

    dsn = require_live_test_database_url()
    from course_insight.infrastructure.postgresql.migration_runner import (
        rebuild_schema_for_tests,
    )
    from course_insight.infrastructure.postgresql.pool import (
        create_postgres_pool,
    )

    pool = create_postgres_pool(
        dsn,
        min_size=1,
        max_size=4,
        connect_timeout_seconds=5,
    )
    try:
        rebuild_schema_for_tests(pool, allow_destructive=True)
        repository = PostgresM0Repository(pool, outbox_clock=lambda: NOW)
        assert repository.schema_is_current()
        yield repository
    finally:
        pool.close()


def test_real_postgres_m0_event_outbox_and_workflow_roundtrip(
    live_repository: PostgresM0Repository,
) -> None:
    event = _event("真实/PG 🐘")
    assert live_repository.append_events([event]) == ((event.event_id,), ())
    assert live_repository.append_events([event]) == ((), (event.event_id,))

    claimed = live_repository.claim_outbox_batch(
        "pg-worker",
        now=NOW,
        lease_until=LEASE,
        batch_size=10,
    )
    assert [record.event_id for record in claimed] == [event.event_id]
    assert live_repository.mark_outbox_delivered(
        "pg-worker",
        [(claimed[0].event_id, claimed[0].version)],
    ) == (event.event_id,)

    pending = _run(operation_id="real-submit", paper_id="real-paper")
    assert live_repository.insert_or_get_assessment_run(pending) == pending
    claimed_run = live_repository.claim_assessment_run(
        pending.operation_id,
        worker_id="workflow-worker",
        now=NOW,
        lease_until=LEASE,
    )
    assert claimed_run is not None
    assert claimed_run.checkpoint == "claimed"


def test_real_postgres_m0_allows_only_one_nonterminal_review_per_paper(
    live_repository: PostgresM0Repository,
) -> None:
    barrier = Barrier(2)
    lock = Lock()
    winners: list[str] = []
    error_codes: list[str] = []
    candidates = (
        _run(
            operation_id="review-live-1",
            operation="review",
            request_checksum="audit-1:v1",
            paper_id="review-paper-live",
        ),
        _run(
            operation_id="review-live-2",
            operation="review",
            request_checksum="audit-1:v2",
            paper_id="review-paper-live",
        ),
    )

    def create_and_claim(candidate: AssessmentRun, worker_id: str) -> None:
        barrier.wait()
        try:
            recorded = live_repository.insert_or_get_assessment_run(candidate)
            claimed = live_repository.claim_assessment_run(
                recorded.operation_id,
                worker_id=worker_id,
                now=NOW,
                lease_until=LEASE,
            )
            assert claimed is not None
            with lock:
                winners.append(recorded.operation_id)
        except DomainError as error:
            with lock:
                error_codes.append(error.code)

    threads = [
        Thread(target=create_and_claim, args=(candidates[0], "pg-worker-a")),
        Thread(target=create_and_claim, args=(candidates[1], "pg-worker-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert error_codes == ["WORKFLOW_BUSY"]
    stored = live_repository.get_assessment_run_by_paper(
        "review-paper-live",
        operation="review",
    )
    assert stored is not None
    assert stored.operation_id == winners[0]
