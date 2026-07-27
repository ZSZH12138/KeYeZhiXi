"""Focused regression tests for deterministic M0 workflow persistence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
    _assert_same_run as postgres_assert_same_run,
    _run_from_row as postgres_run_from_row,
)
from course_insight.infrastructure.postgresql.sqlite_import_destination import (
    _COLUMNS as POSTGRES_IMPORT_COLUMNS,
)
from course_insight.infrastructure.postgresql.sqlite_import_source import (
    read_and_validate_source,
)
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
)
from course_insight.infrastructure.sqlite.workflow_migration import (
    ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL,
    ASSESSMENT_RUNS_SUBMIT_INDEX_SQL,
    ASSESSMENT_RUNS_V6_SQL,
    ASSESSMENT_RUNS_V9_SQL,
)
from course_insight.modules.m0_platform.workflow import AssessmentRun, advance_run
from tests.integration.test_postgres_m0_repository import (
    _Connection,
    _Pool,
    _Step,
)


NOW = datetime(2026, 7, 25, 4, 0, tzinfo=timezone.utc)
LEASE = NOW + timedelta(seconds=30)
EXTENDED_LEASE = NOW + timedelta(seconds=60)


def _run(**updates: object) -> AssessmentRun:
    values: dict[str, object] = {
        "operation_id": "submit-hardening-1",
        "operation": "submit",
        "request_checksum": "request-checksum",
        "course_id": "course-1",
        "class_id": "class-1",
        "learner_id": "learner-1",
        "session_id": "session-1",
        "task_id": "task-1",
        "paper_id": "paper-hardening-1",
        "attempt_id": "attempt-1",
        "feedback_id": None,
        "report_id": None,
        "checkpoint": "pending",
        "status": "pending",
        "version": 1,
        "locked_by": None,
        "lease_until": None,
        "error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
        "knowledge_bundle_id": "bundle-1",
        "knowledge_bundle_version": "1.2.0",
        "knowledge_bundle_checksum": "a" * 64,
        "course_package_id": "package-1",
        "evidence_index_id": "index-1",
        "evidence_index_version": "2026-07-25",
        "evidence_index_checksum": "b" * 64,
        "state_policy_checksum": "c" * 64,
        "teacher_policy_checksum": None,
        "previous_state_frozen": False,
        "previous_learner_snapshot_id": None,
        "previous_learner_state_version": None,
        "previous_class_snapshot_id": None,
        "previous_class_state_version": None,
    }
    values.update(updates)
    return AssessmentRun(**values)  # type: ignore[arg-type]


def _repository(path: Path) -> SQLiteM0Repository:
    repository = SQLiteM0Repository(path)
    repository.initialize()
    return repository


def _policy_fields(*, learned: bool = False) -> dict[str, object]:
    return {
        "policy_id": (
            "learned-policy-v1" if learned else "m6-deterministic-v1"
        ),
        "adapter_id": (
            "linucb-adapter" if learned else "m6-rules-adapter"
        ),
        "adapter_version": "v1",
        "artifact_sha256": "d" * 64 if learned else None,
        "feature_schema_version": "m6-features-v1",
        "action_space_version": "m6-action-space-v1",
        "gate_policy_version": "m6-active-gate-v1",
    }


def _mapping(run: AssessmentRun) -> dict[str, object]:
    return {
        field: getattr(run, field)
        for field in run.__dataclass_fields__
    }


def test_private_dependencies_and_state_checkpoint_are_validated() -> None:
    run = _run(
        checkpoint="events_appended",
        status="running",
        version=4,
        locked_by="worker-1",
        lease_until=LEASE,
    )

    frozen = advance_run(
        run,
        "state_inputs_frozen",
        now=NOW + timedelta(seconds=1),
        previous_state_frozen=True,
        previous_learner_snapshot_id="learner-snapshot-3",
        previous_learner_state_version=3,
        previous_class_snapshot_id="class-snapshot-without-public-version",
        previous_class_state_version=None,
    )

    assert frozen.previous_state_frozen is True
    assert frozen.previous_learner_state_version == 3
    assert frozen.previous_class_snapshot_id == (
        "class-snapshot-without-public-version"
    )
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(knowledge_bundle_checksum="not-a-sha256")
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(
            previous_state_frozen=False,
            previous_learner_snapshot_id="must-not-exist-before-freeze",
            previous_learner_state_version=1,
        )


def test_private_policy_identity_rejects_partial_or_invalid_metadata() -> None:
    complete = _policy_fields()

    assert (
        _run(
            checkpoint="policy_frozen",
            status="running",
            locked_by="worker-1",
            lease_until=LEASE,
            state_version=3,
            previous_state_frozen=True,
            **complete,
        ).policy_id
        == "m6-deterministic-v1"
    )
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(**complete)
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(policy_id="partial-policy")
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(**{**complete, "artifact_sha256": "not-a-sha256"})
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(artifact_sha256="d" * 64)
    with pytest.raises(ValueError, match="workflow metadata"):
        _run(
            operation="review",
            target_audit_id="audit-1",
            target_audit_version=1,
            **complete,
        )


def test_submit_freezes_complete_policy_identity_at_dedicated_checkpoint() -> None:
    state_saved = _run(
        checkpoint="state_saved",
        status="running",
        version=7,
        locked_by="worker-1",
        lease_until=LEASE,
        state_version=3,
        previous_state_frozen=True,
    )

    frozen = advance_run(
        state_saved,
        "policy_frozen",
        now=NOW + timedelta(seconds=1),
        **_policy_fields(learned=True),
    )

    assert frozen.checkpoint == "policy_frozen"
    assert frozen.policy_id == "learned-policy-v1"
    assert frozen.artifact_sha256 == "d" * 64
    assert frozen.version == state_saved.version + 1


def test_sqlite_round_trips_every_recovery_field_and_compares_dependencies(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "workflow.db")
    candidate = _run(
        checkpoint="policy_frozen",
        status="running",
        version=8,
        locked_by="worker-1",
        lease_until=LEASE,
        state_version=3,
        previous_state_frozen=True,
        **_policy_fields(learned=True),
    )

    inserted = repository.insert_or_get_assessment_run(candidate)
    restored = repository.get_assessment_run(candidate.operation_id)
    assert inserted == candidate
    assert restored == candidate
    assert inserted is not candidate
    assert restored is not inserted
    repository.initialize()
    assert repository.schema_is_current()

    changed_dependency = replace(
        candidate,
        evidence_index_checksum="d" * 64,
    )
    with pytest.raises(DomainError) as captured:
        repository.insert_or_get_assessment_run(changed_dependency)
    assert captured.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"


def test_sqlite_policy_checkpoint_atomically_persists_all_seven_fields(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "policy-freeze.db")
    repository.insert_or_get_assessment_run(_run())
    current = repository.claim_assessment_run(
        "submit-hardening-1",
        worker_id="worker-1",
        now=NOW,
        lease_until=LEASE,
    )
    assert current is not None
    for checkpoint in (
        "scoring_saved",
        "events_appended",
        "state_inputs_frozen",
        "state_saved",
    ):
        updates = (
            {"previous_state_frozen": True}
            if checkpoint == "state_inputs_frozen"
            else {"state_version": 3}
            if checkpoint == "state_saved"
            else {}
        )
        current = repository.advance_assessment_run(
            current.operation_id,
            expected_version=current.version,
            checkpoint=checkpoint,
            worker_id="worker-1",
            now=current.updated_at + timedelta(seconds=1),
            **updates,
        )

    frozen = repository.advance_assessment_run(
        current.operation_id,
        expected_version=current.version,
        checkpoint="policy_frozen",
        worker_id="worker-1",
        now=current.updated_at + timedelta(seconds=1),
        **_policy_fields(learned=True),
    )

    assert frozen == repository.get_assessment_run(frozen.operation_id)
    for field, value in _policy_fields(learned=True).items():
        assert getattr(frozen, field) == value


def test_sqlite_v13_schema_rejects_partial_policy_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "partial-policy.db"
    repository = _repository(database_path)
    repository.insert_or_get_assessment_run(_run())

    with connect_sqlite(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE m0_assessment_runs
                SET policy_id = 'partial-policy'
                WHERE operation_id = 'submit-hardening-1'
                """
            )


def test_sqlite_v13_schema_rejects_policy_identity_for_review(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "review-policy.db"
    repository = _repository(database_path)
    repository.insert_or_get_assessment_run(
        _run(
            operation_id="review-hardening-1",
            operation="review",
            target_audit_id="audit-1",
            target_audit_version=1,
        )
    )

    with connect_sqlite(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE m0_assessment_runs
                SET state_version = 3,
                    previous_state_frozen = 1,
                    checkpoint = 'policy_frozen',
                    policy_id = 'm6-deterministic-v1',
                    adapter_id = 'm6-rules-adapter',
                    adapter_version = 'v1',
                    feature_schema_version = 'm6-features-v1',
                    action_space_version = 'm6-action-space-v1',
                    gate_policy_version = 'm6-active-gate-v1'
                WHERE operation_id = 'review-hardening-1'
                """
            )


def test_sqlite_v13_schema_rejects_policy_identity_before_checkpoint(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "early-policy.db"
    repository = _repository(database_path)
    repository.insert_or_get_assessment_run(_run())

    with connect_sqlite(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE m0_assessment_runs
                SET policy_id = 'm6-deterministic-v1',
                    adapter_id = 'm6-rules-adapter',
                    adapter_version = 'v1',
                    feature_schema_version = 'm6-features-v1',
                    action_space_version = 'm6-action-space-v1',
                    gate_policy_version = 'm6-active-gate-v1'
                WHERE operation_id = 'submit-hardening-1'
                """
            )


def test_sqlite_renewal_is_cas_and_expired_owner_cannot_write(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "workflow.db")
    repository.insert_or_get_assessment_run(_run())
    claimed = repository.claim_assessment_run(
        "submit-hardening-1",
        worker_id="worker-1",
        now=NOW,
        lease_until=LEASE,
    )
    assert claimed is not None

    assert repository.renew_assessment_run_lease(
        claimed.operation_id,
        expected_version=claimed.version,
        worker_id="worker-1",
        now=NOW + timedelta(seconds=10),
        lease_until=EXTENDED_LEASE,
    )
    renewed = repository.get_assessment_run(claimed.operation_id)
    assert renewed is not None
    assert renewed.version == claimed.version
    assert renewed.lease_until == EXTENDED_LEASE
    assert not repository.renew_assessment_run_lease(
        claimed.operation_id,
        expected_version=claimed.version,
        worker_id="other-worker",
        now=NOW + timedelta(seconds=11),
        lease_until=EXTENDED_LEASE + timedelta(seconds=1),
    )

    with pytest.raises(DomainError) as advance_error:
        repository.advance_assessment_run(
            claimed.operation_id,
            expected_version=claimed.version,
            checkpoint="scoring_saved",
            worker_id="worker-1",
            now=EXTENDED_LEASE,
        )
    with pytest.raises(DomainError) as fail_error:
        repository.fail_assessment_run(
            claimed.operation_id,
            expected_version=claimed.version,
            worker_id="worker-1",
            error_code="SAFE_FAILURE",
            now=EXTENDED_LEASE,
        )
    assert advance_error.value.code == "WORKFLOW_LEASE_LOST"
    assert fail_error.value.code == "WORKFLOW_LEASE_LOST"


def test_sqlite_freezes_state_inputs_atomically_and_reclaim_preserves_them(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "workflow.db")
    repository.insert_or_get_assessment_run(_run())
    current = repository.claim_assessment_run(
        "submit-hardening-1",
        worker_id="worker-1",
        now=NOW,
        lease_until=LEASE,
    )
    assert current is not None
    for checkpoint in ("scoring_saved", "events_appended"):
        current = repository.advance_assessment_run(
            current.operation_id,
            expected_version=current.version,
            checkpoint=checkpoint,
            worker_id="worker-1",
            now=current.updated_at + timedelta(seconds=1),
        )
    frozen = repository.advance_assessment_run(
        current.operation_id,
        expected_version=current.version,
        checkpoint="state_inputs_frozen",
        worker_id="worker-1",
        now=current.updated_at + timedelta(seconds=1),
        previous_state_frozen=True,
        previous_learner_snapshot_id="learner-snapshot-7",
        previous_learner_state_version=7,
        previous_class_snapshot_id="class-snapshot-4",
        previous_class_state_version=None,
    )

    reclaimed = repository.reclaim_assessment_run(
        frozen.operation_id,
        worker_id="worker-2",
        now=LEASE,
        lease_until=LEASE + timedelta(seconds=30),
    )

    assert reclaimed.checkpoint == "state_inputs_frozen"
    assert reclaimed.previous_state_frozen is True
    assert reclaimed.previous_learner_snapshot_id == "learner-snapshot-7"
    assert reclaimed.previous_learner_state_version == 7
    assert reclaimed.previous_class_snapshot_id == "class-snapshot-4"
    assert reclaimed.previous_class_state_version is None


def test_sqlite_v9_migration_preserves_v8_rows_as_explicit_legacy(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute(
            "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
        )
        connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
        connection.execute("DROP TABLE m0_assessment_runs")
        connection.execute(ASSESSMENT_RUNS_V6_SQL)
        connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
        connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)
        connection.execute("DELETE FROM schema_migrations WHERE version >= 9")
        connection.execute(
            """
            INSERT INTO m0_assessment_runs(
                operation_id, operation, request_checksum,
                course_id, class_id, learner_id, session_id,
                task_id, paper_id, attempt_id, checkpoint, status, version,
                created_at, updated_at
            ) VALUES (
                'legacy-v8', 'submit', 'legacy-checksum',
                'course-1', 'class-1', 'learner-1', 'session-1',
                'task-1', 'paper-legacy', 'attempt-legacy',
                'events_appended', 'failed', 4,
                '2026-07-25T04:00:00+00:00',
                '2026-07-25T04:01:00+00:00'
            )
            """
        )

        migrate(connection)

        row = connection.execute(
            """
            SELECT knowledge_bundle_id, evidence_index_id,
                   previous_state_frozen, previous_learner_snapshot_id,
                   previous_class_snapshot_id
            FROM m0_assessment_runs
            WHERE operation_id = 'legacy-v8'
            """
        ).fetchone()
        assert current_schema_version(connection) == SCHEMA_VERSION
        assert tuple(row) == (None, None, None, None, None)


def test_sqlite_v13_migration_preserves_v12_rows_with_null_policy_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow-v12.db"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute(
            "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
        )
        connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
        connection.execute("DROP TABLE m0_assessment_runs")
        connection.execute(ASSESSMENT_RUNS_V9_SQL)
        connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
        connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)
        connection.execute("DELETE FROM schema_migrations WHERE version >= 13")
        connection.execute(
            """
            INSERT INTO m0_assessment_runs(
                operation_id, operation, request_checksum,
                course_id, class_id, learner_id, session_id,
                task_id, paper_id, attempt_id, checkpoint, status, version,
                created_at, updated_at
            ) VALUES (
                'legacy-v12', 'submit', 'legacy-checksum',
                'course-1', 'class-1', 'learner-1', 'session-1',
                'task-1', 'paper-v12', 'attempt-v12',
                'state_saved', 'failed', 7,
                '2026-07-25T04:00:00+00:00',
                '2026-07-25T04:01:00+00:00'
            )
            """
        )

        migrate(connection)

        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info('m0_assessment_runs')"
            ).fetchall()
        }
        row = connection.execute(
            """
            SELECT policy_id, adapter_id, adapter_version, artifact_sha256,
                   feature_schema_version, action_space_version,
                   gate_policy_version
            FROM m0_assessment_runs
            WHERE operation_id = 'legacy-v12'
            """
        ).fetchone()
        assert SCHEMA_VERSION == 13
        assert current_schema_version(connection) == 13
        assert {
            "policy_id",
            "adapter_id",
            "adapter_version",
            "artifact_sha256",
            "feature_schema_version",
            "action_space_version",
            "gate_policy_version",
        } <= columns
        assert tuple(row) == (None, None, None, None, None, None, None)


def test_sqlite_to_postgres_import_preserves_v9_recovery_metadata(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(database_path)
    candidate = _run(
        checkpoint="policy_frozen",
        status="running",
        version=8,
        locked_by="worker-1",
        lease_until=LEASE,
        state_version=3,
        previous_state_frozen=True,
        **_policy_fields(learned=True),
    )
    repository.insert_or_get_assessment_run(candidate)

    rows_by_table, _, _ = read_and_validate_source(database_path)

    prepared = rows_by_table["m0_assessment_runs"][0]
    assert prepared.columns == POSTGRES_IMPORT_COLUMNS["m0_assessment_runs"]
    assert prepared.value_for("knowledge_bundle_id") == "bundle-1"
    assert prepared.value_for("evidence_index_checksum") == "b" * 64
    assert prepared.value_for("previous_state_frozen") is True
    for field, value in _policy_fields(learned=True).items():
        assert prepared.value_for(field) == value


def test_postgres_decoder_and_renewal_sql_cover_new_private_fields() -> None:
    running = _run(
        checkpoint="policy_frozen",
        status="running",
        version=8,
        locked_by="worker-1",
        lease_until=LEASE,
        state_version=3,
        previous_state_frozen=True,
        **_policy_fields(learned=True),
    )
    assert postgres_run_from_row(_mapping(running)) == running
    with pytest.raises(DomainError) as dependency_conflict:
        postgres_assert_same_run(
            running,
            replace(running, knowledge_bundle_checksum="d" * 64),
        )
    assert dependency_conflict.value.code == "ASSESSMENT_SUBMISSION_CONFLICT"

    def check(parameters: tuple[object, ...]) -> None:
        assert parameters == (
            EXTENDED_LEASE,
            NOW + timedelta(seconds=10),
            running.operation_id,
            "worker-1",
            running.version,
            NOW + timedelta(seconds=10),
        )

    connection = _Connection(
        [
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "status = 'running'",
                    "lease_until > %s",
                    "RETURNING operation_id",
                ),
                one={"operation_id": running.operation_id},
                rowcount=1,
                check=check,
            )
        ]
    )

    renewed = PostgresM0Repository(_Pool(connection)).renew_assessment_run_lease(
        running.operation_id,
        expected_version=running.version,
        worker_id="worker-1",
        now=NOW + timedelta(seconds=10),
        lease_until=EXTENDED_LEASE,
    )

    assert renewed is True


def test_postgres_policy_checkpoint_writes_all_seven_frozen_fields() -> None:
    current = _run(
        checkpoint="state_saved",
        status="running",
        version=7,
        locked_by="worker-1",
        lease_until=LEASE,
        state_version=3,
        previous_state_frozen=True,
    )

    def check(parameters: tuple[object, ...]) -> None:
        for value in _policy_fields(learned=True).values():
            assert value in parameters

    connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_mapping(current),
            ),
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "policy_id = %s",
                    "artifact_sha256 = %s",
                    "gate_policy_version = %s",
                ),
                rowcount=1,
                check=check,
            ),
        ]
    )

    frozen = PostgresM0Repository(_Pool(connection)).advance_assessment_run(
        current.operation_id,
        expected_version=current.version,
        checkpoint="policy_frozen",
        worker_id="worker-1",
        now=NOW + timedelta(seconds=1),
        **_policy_fields(learned=True),
    )

    assert frozen.checkpoint == "policy_frozen"
    assert frozen.policy_id == "learned-policy-v1"


def test_postgres_expired_owner_cannot_advance_or_fail() -> None:
    expired = _run(
        checkpoint="claimed",
        status="running",
        version=2,
        locked_by="worker-1",
        lease_until=LEASE,
    )
    advance_connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_mapping(expired),
            )
        ]
    )
    fail_connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_mapping(expired),
            )
        ]
    )
    repository = PostgresM0Repository(
        _Pool(advance_connection, fail_connection)
    )

    with pytest.raises(DomainError) as advance_error:
        repository.advance_assessment_run(
            expired.operation_id,
            expected_version=expired.version,
            checkpoint="scoring_saved",
            worker_id="worker-1",
            now=LEASE,
        )
    with pytest.raises(DomainError) as fail_error:
        repository.fail_assessment_run(
            expired.operation_id,
            expected_version=expired.version,
            worker_id="worker-1",
            error_code="SAFE_FAILURE",
            now=LEASE,
        )

    assert advance_error.value.code == "WORKFLOW_LEASE_LOST"
    assert fail_error.value.code == "WORKFLOW_LEASE_LOST"


def test_postgres_state_input_checkpoint_writes_frozen_references() -> None:
    current = _run(
        checkpoint="events_appended",
        status="running",
        version=4,
        locked_by="worker-1",
        lease_until=LEASE,
    )

    def check(parameters: tuple[object, ...]) -> None:
        assert True in parameters
        assert "learner-snapshot-9" in parameters
        assert 9 in parameters
        assert "class-snapshot-5" in parameters

    connection = _Connection(
        [
            _Step(
                ("WHERE operation_id = %s", "FOR UPDATE"),
                one=_mapping(current),
            ),
            _Step(
                (
                    "UPDATE m0_assessment_runs",
                    "previous_state_frozen = %s",
                    "previous_class_snapshot_id = %s",
                ),
                rowcount=1,
                check=check,
            ),
        ]
    )

    frozen = PostgresM0Repository(_Pool(connection)).advance_assessment_run(
        current.operation_id,
        expected_version=current.version,
        checkpoint="state_inputs_frozen",
        worker_id="worker-1",
        now=NOW + timedelta(seconds=1),
        previous_state_frozen=True,
        previous_learner_snapshot_id="learner-snapshot-9",
        previous_learner_state_version=9,
        previous_class_snapshot_id="class-snapshot-5",
        previous_class_state_version=None,
    )

    assert frozen.previous_state_frozen is True
    assert frozen.previous_class_state_version is None
