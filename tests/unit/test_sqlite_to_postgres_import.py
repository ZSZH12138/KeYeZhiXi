from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.postgresql.sqlite_import import (
    MigrationError,
    PostgresImportDestination,
    PreparedImportRow,
    SQLiteToPostgresMigrator,
)
from course_insight.infrastructure.postgresql.sqlite_import import (
    _validate_contract_identity,
)
from course_insight.infrastructure.postgresql.sqlite_import_cli import (
    main as migration_cli_main,
)
from course_insight.infrastructure.sqlite import SCHEMA_VERSION
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.modules.m0_platform.workflow import AssessmentRun


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


def _plan(seed: str) -> tuple[TaskPlan, str]:
    idempotency_key = seed * 64
    return (
        TaskPlan(
            task_id=f"task_{idempotency_key}",
            task_type="stage_assessment",
            course_id=f"course_{seed}",
            class_id="class_1",
            learner_id="learner_1",
            session_id=f"session_{seed}",
            blueprint_id="blueprint_1",
            knowledge_bundle_id="bundle_1",
            course_package_id="package_1",
            workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
            next_module="M8",
            created_at=NOW,
        ),
        idempotency_key,
    )


def _source_with_plans(path: Path, seeds: str = "a") -> Path:
    repository = SQLiteM4Repository(path)
    repository.initialize()
    for seed in seeds:
        plan, key = _plan(seed)
        repository.save_task_plan(plan, key)
    return path


class _MemoryDestination:
    def __init__(self, *, fail_identity: tuple[object, ...] | None = None):
        self.rows: dict[
            tuple[str, tuple[object, ...]], PreparedImportRow
        ] = {}
        self.fail_identity = fail_identity
        self.apply_calls = 0
        self.verify_calls = 0

    def apply_batch(
        self,
        table: str,
        rows: tuple[PreparedImportRow, ...],
    ) -> None:
        self.apply_calls += 1
        snapshot = deepcopy(self.rows)
        try:
            for row in rows:
                if row.identity == self.fail_identity:
                    raise RuntimeError("password=hunter2 /private/source.sqlite3")
                key = (table, row.identity)
                existing = self.rows.get(key)
                if existing is not None and existing.fingerprint != row.fingerprint:
                    raise RuntimeError("conflict")
                self.rows[key] = deepcopy(row)
        except Exception:
            self.rows = snapshot
            raise

    def verify_batch(
        self,
        table: str,
        rows: tuple[PreparedImportRow, ...],
    ) -> None:
        self.verify_calls += 1
        for row in rows:
            stored = self.rows.get((table, row.identity))
            if stored is None or stored.fingerprint != row.fingerprint:
                raise RuntimeError("verification failed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dry_run_validates_without_writing_and_writes_safe_report(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3")
    before = _sha256(source)
    destination = _MemoryDestination()
    report_path = tmp_path / "report.json"

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
    ).run(mode="dry-run", batch_size=2, report_path=report_path)

    assert report.status == "validated"
    assert report.fully_contract_validated is True
    assert destination.apply_calls == 0
    assert destination.verify_calls == 0
    assert _sha256(source) == before
    serialized = report_path.read_text(encoding="utf-8")
    assert str(source.resolve()) not in serialized
    assert "postgresql://" not in serialized
    assert "learner_1" not in serialized
    assert f"task_{'a' * 64}" not in serialized
    assert '"payload"' not in serialized
    decoded = json.loads(serialized)
    m4 = next(
        table
        for table in decoded["tables"]
        if table["table"] == "m4_task_plans"
    )
    assert m4["source_count"] == 1
    assert len(m4["identity_digest"]) == 64
    assert decoded["partial_envelope_rows"] == 0
    assert {
        table["table"]
        for table in decoded["tables"]
        if table["table"].startswith("m6_policy_")
    } == {
        "m6_policy_artifacts",
        "m6_policy_executions",
        "m6_policy_observations",
        "m6_policy_rewards",
        "m6_policy_evaluations",
    }


def test_apply_is_batched_verified_and_idempotent(tmp_path: Path) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination()
    migrator = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
    )

    first = migrator.run(mode="apply", batch_size=1)
    second = migrator.run(mode="apply", batch_size=1)

    assert first.status == second.status == "completed"
    assert first.completed_batches == second.completed_batches == 2
    assert len(destination.rows) == 2
    assert destination.apply_calls == 4
    assert destination.verify_calls == 0


def test_policy_rows_are_validated_and_imported_in_dependency_order(
    tmp_path: Path,
) -> None:
    from tests.integration.test_m6_persistence import (
        _inputs,
        _policy_artifact,
        _policy_execution,
        _repository,
        _service,
    )
    from course_insight.modules.m6_tutoring_fsm.policy_types import (
        PolicyEvaluationRecord,
        PolicyRewardRecord,
    )

    source = tmp_path / "policy-source.sqlite3"
    repository = _repository(source)
    artifact = _policy_artifact()
    execution = _policy_execution(request_fingerprint="f" * 64)
    reward = PolicyRewardRecord(
        policy_execution_fingerprint=execution.policy_execution_fingerprint,
        outcome_identity="outcome_import",
        status="observed",
        reward=0.9,
    )
    evaluation = PolicyEvaluationRecord(
        policy_id=artifact.policy_id,
        dataset_identity="dataset_import",
        status="sufficient_data",
        approved=True,
        effective_sample_size=20.0,
        action_coverage=1.0,
    )
    repository.save_policy_artifact(artifact)
    repository.commit_policy_execution(execution)
    repository.save_policy_reward(reward)
    repository.save_policy_evaluation(evaluation)
    _service(repository).decide_next_action(*_inputs(), None)
    destination = _MemoryDestination()

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
    ).run(mode="apply", batch_size=10)

    counts = {table.table: table.source_count for table in report.tables}
    assert counts["m6_policy_artifacts"] == 1
    assert counts["m6_policy_executions"] == 2
    assert counts["m6_policy_observations"] == 1
    assert counts["m6_policy_rewards"] == 1
    assert counts["m6_policy_evaluations"] == 1
    applied_order = [
        table
        for table, _ in destination.rows
        if table.startswith("m6_policy_")
    ]
    assert applied_order.index("m6_policy_executions") < applied_order.index(
        "m6_policy_observations"
    )


def test_failed_batch_rolls_back_and_report_redacts_exception(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination(
        fail_identity=(f"task_{'b' * 64}",)
    )
    report_path = tmp_path / "report.json"

    with pytest.raises(MigrationError, match="MIGRATION_BATCH_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
        ).run(mode="apply", batch_size=2, report_path=report_path)

    assert destination.rows == {}
    report = report_path.read_text(encoding="utf-8")
    assert "hunter2" not in report
    assert "private" not in report
    assert json.loads(report)["error_code"] == "MIGRATION_BATCH_FAILED"


def test_apply_uses_snapshot_checksum_from_reader_not_pre_read_file_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3")
    destination = _MemoryDestination()
    expected_checksum = "c" * 64
    row = _prepared_plan_row()

    from course_insight.infrastructure.postgresql import sqlite_import

    monkeypatch.setattr(
        sqlite_import,
        "_file_checksum",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("pre-read file hash must not be used")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        sqlite_import,
        "_read_and_validate_source",
        lambda _path: (
            {table: ((row,) if table == "m4_task_plans" else ()) for table in (
                "m0_learning_events",
                "m0_event_outbox",
                "m0_assessment_runs",
                "m4_task_plans",
                "m5_learner_states",
                "m5_class_states",
                "m5_state_updates",
                "m6_session_states",
                "m6_policy_artifacts",
                "m6_policy_executions",
                "m6_tutoring_decisions",
                "m6_policy_observations",
                "m6_policy_rewards",
                "m6_policy_evaluations",
                "m7_student_feedback",
                "m8_assessment_papers",
                "m8_score_audits",
                "m8_scoring_results",
                "m9_teacher_reviews",
                "m9_teacher_analytics",
            )},
            0,
            expected_checksum,
        ),
    )

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
    ).run(mode="dry-run")

    assert report.source_file_checksum == expected_checksum


def test_invalid_contract_fails_before_any_destination_write(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3")
    connection = __import__("sqlite3").connect(source)
    try:
        connection.execute(
            """
            UPDATE m4_task_plans
            SET payload = json_set(payload, '$.next_module', 'M9')
            """
        )
        connection.commit()
    finally:
        connection.close()
    destination = _MemoryDestination()

    with pytest.raises(MigrationError, match="SOURCE_VALIDATION_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
        ).run(mode="apply")

    assert destination.apply_calls == 0


def test_dry_run_rejects_tampered_source_table_shape(tmp_path: Path) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")
    connection = __import__("sqlite3").connect(source)
    try:
        connection.execute(
            "ALTER TABLE m4_task_plans ADD COLUMN injected TEXT"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationError, match="SOURCE_VALIDATION_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=_MemoryDestination(),
        ).run(mode="dry-run")


@pytest.mark.parametrize(
    "tamper_sql",
    [
        f"DELETE FROM schema_migrations WHERE version = {SCHEMA_VERSION}",
        "DROP TABLE m7_student_feedback",
    ],
)
def test_dry_run_rejects_incomplete_source_schema(
    tmp_path: Path,
    tamper_sql: str,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")
    connection = __import__("sqlite3").connect(source)
    try:
        connection.execute(tamper_sql)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MigrationError, match="SOURCE_VALIDATION_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=_MemoryDestination(),
        ).run(mode="dry-run")


def test_historical_event_without_outbox_is_truthfully_partial(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")
    connection = __import__("sqlite3").connect(source)
    try:
        connection.execute(
            """
            INSERT INTO m0_learning_events(
                event_id, idempotency_key, event_type, occurred_at, payload
            ) VALUES (?, ?, ?, ?, json(?))
            """,
            (
                "event-delivered",
                "event-delivered",
                "assessment_submitted",
                NOW.isoformat(),
                '{"score":1}',
            ),
        )
        connection.commit()
    finally:
        connection.close()

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=_MemoryDestination(),
    ).run(mode="dry-run")

    assert report.partial_envelope_rows == 1
    assert report.fully_contract_validated is False
    assert report.status == "validated_with_source_limitations"

    applied = SQLiteToPostgresMigrator(
        source_path=source,
        destination=_MemoryDestination(),
    ).run(mode="apply")
    assert applied.status == "completed_with_source_limitations"


def test_apply_preserves_full_outbox_and_workflow_state(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    repository = SQLiteM0Repository(source, outbox_clock=lambda: NOW)
    repository.initialize()
    event = LearningEvent(
        event_id="事件-1",
        event_type="assessment_submitted",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        attempt_id="attempt_1",
        payload={"score": 1},
        occurred_at=NOW,
    )
    repository.append_events((event,))
    claimed = repository.claim_outbox_batch(
        "worker-1",
        now=NOW,
        lease_until=NOW + timedelta(seconds=30),
        batch_size=1,
    )
    assert len(claimed) == 1
    run = AssessmentRun(
        operation_id="operation_1",
        operation="start",
        request_checksum="a" * 64,
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        task_id="task_1",
        paper_id="paper_1",
        attempt_id=None,
        feedback_id=None,
        report_id=None,
        checkpoint="pending",
        status="pending",
        version=1,
        locked_by=None,
        lease_until=None,
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    repository.insert_or_get_assessment_run(run)
    destination = _MemoryDestination()

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
    ).run(mode="apply")

    assert report.fully_contract_validated is True
    outbox = destination.rows[("m0_event_outbox", ("事件-1",))]
    assert outbox.value_for("status") == "processing"
    assert outbox.value_for("attempt_count") == 1
    assert outbox.value_for("version") == claimed[0].version
    assert outbox.value_for("locked_by") == "worker-1"
    workflow = destination.rows[
        ("m0_assessment_runs", ("operation_1",))
    ]
    assert workflow.value_for("checkpoint") == "pending"
    assert workflow.value_for("status") == "pending"
    assert workflow.value_for("version") == 1


def test_mode_and_batch_size_fail_closed(tmp_path: Path) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")
    migrator = SQLiteToPostgresMigrator(
        source_path=source,
        destination=_MemoryDestination(),
    )

    with pytest.raises(ValueError, match="mode"):
        migrator.run(mode="yes")
    with pytest.raises(ValueError, match="batch_size"):
        migrator.run(mode="dry-run", batch_size=0)


class _Result:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = deepcopy(row)

    def fetchone(self) -> dict[str, Any] | None:
        return deepcopy(self._row)


class _Transaction:
    def __init__(self, connection: "_FakePostgresConnection") -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.transaction_count += 1

    def __exit__(self, error_type, error, traceback) -> None:
        del error, traceback
        if error_type is not None:
            self._connection.rollback_count += 1


class _FakePostgresConnection:
    def __init__(self, selected: dict[str, Any]) -> None:
        self.selected = selected
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_count = 0
        self.rollback_count = 0

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> _Result:
        self.executed.append((statement, parameters))
        if statement.lstrip().startswith("SELECT"):
            return _Result(self.selected)
        return _Result()


class _ConnectionContext:
    def __init__(self, connection: _FakePostgresConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakePostgresConnection:
        return self._connection

    def __exit__(self, *_: Any) -> None:
        return None


class _FakePool:
    def __init__(self, connection: _FakePostgresConnection) -> None:
        self.connection_value = connection
        self.connection_count = 0

    def connection(self) -> _ConnectionContext:
        self.connection_count += 1
        return _ConnectionContext(self.connection_value)


def _prepared_plan_row() -> PreparedImportRow:
    plan, key = _plan("a")
    columns = (
        "task_id",
        "idempotency_key",
        "payload",
        "payload_checksum",
        "schema_version",
    )
    values = (
        plan.task_id,
        key,
        json.dumps(
            plan.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        plan.content_checksum(),
        plan.schema_version,
    )
    return PreparedImportRow(
        table="m4_task_plans",
        columns=columns,
        values=values,
        identity=(plan.task_id,),
        version=(plan.schema_version,),
        checksum=plan.content_checksum(),
        fingerprint="f" * 64,
        contract_validated=True,
    )


def test_postgres_destination_uses_one_transaction_and_fixed_allowlist() -> None:
    row = _prepared_plan_row()
    selected = dict(zip(row.columns, row.values, strict=True))
    selected["payload"] = json.loads(selected["payload"])
    connection = _FakePostgresConnection(selected)
    pool = _FakePool(connection)
    destination = PostgresImportDestination(pool)

    destination.apply_batch("m4_task_plans", (row,))

    assert connection.transaction_count == 1
    assert connection.rollback_count == 0
    assert any(
        statement.lstrip().startswith("INSERT INTO m4_task_plans")
        for statement, _ in connection.executed
    )
    with pytest.raises(ValueError, match="allowlist"):
        destination.apply_batch("m4_task_plans; DROP TABLE users", (row,))
    assert pool.connection_count == 1


def test_postgres_destination_rolls_back_batch_on_mismatch() -> None:
    row = _prepared_plan_row()
    selected = dict(zip(row.columns, row.values, strict=True))
    selected["payload"] = json.loads(selected["payload"])
    selected["payload_checksum"] = "0" * 64
    connection = _FakePostgresConnection(selected)

    with pytest.raises(MigrationError, match="TARGET_VERIFICATION_FAILED"):
        PostgresImportDestination(_FakePool(connection)).apply_batch(
            "m4_task_plans",
            (row,),
        )

    assert connection.transaction_count == 1
    assert connection.rollback_count == 1


def test_postgres_destination_supports_post_commit_verify_and_empty_batches() -> None:
    row = _prepared_plan_row()
    selected = dict(zip(row.columns, row.values, strict=True))
    selected["payload"] = json.loads(selected["payload"])
    connection = _FakePostgresConnection(selected)
    destination = PostgresImportDestination(_FakePool(connection))

    destination.verify_batch("m4_task_plans", (row,))
    destination.apply_batch("m4_task_plans", ())
    destination.verify_batch("m4_task_plans", ())

    assert connection.transaction_count == 1
    invalid_columns = PreparedImportRow(
        table=row.table,
        columns=("task_id",),
        values=(row.identity[0],),
        identity=row.identity,
        version=row.version,
        checksum=row.checksum,
        fingerprint=row.fingerprint,
        contract_validated=True,
    )
    with pytest.raises(ValueError, match="allowlist"):
        destination.apply_batch("m4_task_plans", (invalid_columns,))


def test_cli_defaults_to_dry_run_without_database_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")
    report = tmp_path / "report.json"

    exit_code = migration_cli_main(
        ["--source", str(source), "--report", str(report)],
        environment={},
    )

    assert exit_code == 0
    assert json.loads(report.read_text(encoding="utf-8"))["mode"] == "dry-run"
    output = capsys.readouterr().out
    assert str(source.resolve()) not in output


def test_cli_apply_requires_database_url_without_echoing_environment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")

    exit_code = migration_cli_main(
        [
            "--source",
            str(source),
            "--report",
            str(tmp_path / "report.json"),
            "--apply",
        ],
        environment={"UNRELATED_SECRET": "do-not-print"},
    )

    assert exit_code == 2
    output = capsys.readouterr().err
    assert "MIGRATION_CONFIGURATION_INVALID" in output
    assert "do-not-print" not in output


def test_cli_redacts_invalid_platform_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "")
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "app.json").write_text(
        '{"database":{"backend":"postgresql","url_env":"invalid-name"}}',
        encoding="utf-8",
    )

    exit_code = migration_cli_main(
        [
            "--source",
            str(source),
            "--report",
            str(tmp_path / "report.json"),
            "--project-root",
            str(project_root),
            "--apply",
        ],
        environment={"invalid-name": "postgresql://must-not-print"},
    )

    assert exit_code == 2
    output = capsys.readouterr().err
    assert "MIGRATION_CONFIGURATION_INVALID" in output
    assert "must-not-print" not in output


def test_cli_explicit_apply_runs_validation_then_closes_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from course_insight.infrastructure.postgresql import sqlite_import_cli

    source = _source_with_plans(tmp_path / "source.sqlite3")
    destination = _MemoryDestination()
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "app.json").write_text(
        json.dumps(
            {
                "database": {
                    "backend": "postgresql",
                    "url_env": "MIGRATION_DATABASE_URL",
                }
            }
        ),
        encoding="utf-8",
    )

    class _Pool:
        closed = False

        def close(self) -> None:
            self.closed = True

    pool = _Pool()
    migrations: list[object] = []
    monkeypatch.setattr(
        sqlite_import_cli,
        "create_postgres_pool",
        lambda dsn, **kwargs: (
            pool if dsn == "postgresql://configured" else None
        ),
    )
    monkeypatch.setattr(
        sqlite_import_cli,
        "run_migrations",
        lambda value: migrations.append(value),
    )
    monkeypatch.setattr(
        sqlite_import_cli,
        "PostgresImportDestination",
        lambda value: destination if value is pool else None,
    )

    exit_code = migration_cli_main(
        [
            "--source",
            str(source),
            "--report",
            str(tmp_path / "report.json"),
            "--project-root",
            str(project_root),
            "--apply",
        ],
        environment={
            "DATABASE_URL": "postgresql://must-not-be-used",
            "MIGRATION_DATABASE_URL": "postgresql://configured",
        },
    )

    assert exit_code == 0
    assert migrations == [pool]
    assert pool.closed is True
    assert len(destination.rows) == 1


def test_internal_scope_columns_do_not_require_nonexistent_public_fields() -> None:
    paper = SimpleNamespace(
        paper_id="paper_1",
        task_id="task_1",
        learner_id="learner_1",
    )
    _validate_contract_identity(
        "m8_assessment_papers",
        {
            "paper_id": "paper_1",
            "task_id": "task_1",
            "course_id": "course_1",
            "class_id": "class_1",
            "learner_id": "learner_1",
        },
        paper,
    )
    generated_at = NOW
    analytics = SimpleNamespace(
        report_id="report_1",
        class_report=SimpleNamespace(class_id="class_1"),
        individual_reports=[
            SimpleNamespace(learner_id="learner_b"),
            SimpleNamespace(learner_id="learner_a"),
        ],
        generated_at=generated_at,
    )
    _validate_contract_identity(
        "m9_teacher_analytics",
        {
            "report_id": "report_1",
            "course_id": "course_1",
            "class_id": "class_1",
            "generated_at": generated_at.isoformat(),
            "learner_ids": '["learner_a","learner_b"]',
        },
        analytics,
    )


def test_scoring_result_key_uses_latest_audit_versions_not_payload_checksum() -> None:
    bundle = SimpleNamespace(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        finalized_at=NOW,
        score_audit_records=[
            SimpleNamespace(audit_id="audit_b", audit_version=1),
            SimpleNamespace(audit_id="audit_a", audit_version=1),
            SimpleNamespace(audit_id="audit_a", audit_version=2),
        ],
        content_checksum=lambda: "f" * 64,
    )

    _validate_contract_identity(
        "m8_scoring_results",
        {
            "attempt_id": "attempt_1",
            "result_key": (
                '[{"audit_id":"audit_a","audit_version":2},'
                '{"audit_id":"audit_b","audit_version":1}]'
            ),
            "paper_id": "paper_1",
            "learner_id": "learner_1",
            "finalized_at": NOW.isoformat(),
        },
        bundle,
    )


def test_m4_source_identity_must_match_postgres_sha256_invariant() -> None:
    invalid = SimpleNamespace(task_id="task_short")

    with pytest.raises(ValueError, match="identity"):
        _validate_contract_identity(
            "m4_task_plans",
            {"task_id": "task_short", "idempotency_key": "short"},
            invalid,
        )
