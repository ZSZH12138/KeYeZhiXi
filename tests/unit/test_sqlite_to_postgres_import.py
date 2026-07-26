from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.postgresql.sqlite_import import (
    MigrationError,
    PostgresImportDestination,
    PreparedImportRow,
    SQLiteToPostgresMigrator,
)
from course_insight.infrastructure.postgresql import sqlite_import
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
from course_insight.modules.m4_task_orchestration.intent import IntentStatus
from course_insight.modules.m4_task_orchestration.intent_service import (
    StoredIntentDecision,
)


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


def _decision(
    seed: str,
    *,
    status: IntentStatus = IntentStatus.ACCEPTED,
    integer_scores: bool = False,
) -> StoredIntentDecision:
    accepted = status is IntentStatus.ACCEPTED
    confidence = 1 if integer_scores else 0.91
    margin = 0 if integer_scores else 0.31
    shadow_confidence = 1 if integer_scores else 0.82
    shadow_margin = 0 if integer_scores else 0.22
    return StoredIntentDecision(
        request_key=seed * 64,
        resolved_task_type="practice" if accepted else None,
        decision_status=status,
        decision_source="active_model" if accepted else "refusal",
        adapter_id="test-adapter",
        adapter_version=f"adapter-{seed}",
        policy_version="intent-policy-v1",
        confidence=confidence if accepted else None,
        margin=margin if accepted else None,
        input_checksum=hashlib.sha256(
            f"private-{seed}".encode("utf-8")
        ).hexdigest(),
        reason_codes=("model_accepted",) if accepted else ("unsupported_hint",),
        created_at=NOW + timedelta(seconds=ord(seed)),
        shadow_label="qa" if accepted else None,
        shadow_status=IntentStatus.ACCEPTED if accepted else None,
        shadow_adapter_id="shadow-adapter" if accepted else None,
        shadow_adapter_version="shadow-v1" if accepted else None,
        shadow_confidence=shadow_confidence if accepted else None,
        shadow_margin=shadow_margin if accepted else None,
        shadow_reason_codes=("shadow_accepted",) if accepted else (),
        shadow_agrees=False if accepted else None,
        _generate_checksum=True,
    )


def _legacy_v1_integer_score_checksum(
    decision: StoredIntentDecision,
) -> str:
    payload = decision.canonical_payload()
    for field_name in ("confidence", "margin"):
        value = payload[field_name]
        if type(value) is float and value.is_integer():
            payload[field_name] = int(value)
    shadow = payload["shadow"]
    if type(shadow) is dict:
        for field_name in ("confidence", "margin"):
            value = shadow[field_name]
            if type(value) is float and value.is_integer():
                shadow[field_name] = int(value)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _source_with_intents(path: Path, seeds: str = "a") -> Path:
    repository = SQLiteM4Repository(path)
    repository.initialize()
    for seed in seeds:
        repository.insert_or_get_intent_decision(_decision(seed))
    return path


class _MemoryDestination:
    def __init__(self, *, fail_identity: tuple[object, ...] | None = None):
        self.rows: dict[
            tuple[str, tuple[object, ...]], PreparedImportRow
        ] = {}
        self.fail_identity = fail_identity
        self.apply_calls = 0
        self.verify_calls = 0
        self.applied_batches: list[
            tuple[str, tuple[tuple[object, ...], ...]]
        ] = []

    def apply_batch(
        self,
        table: str,
        rows: tuple[PreparedImportRow, ...],
    ) -> None:
        self.apply_calls += 1
        self.applied_batches.append(
            (table, tuple(row.identity for row in rows))
        )
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


def test_import_manifest_includes_exact_m4_intent_columns() -> None:
    assert "m4_intent_decisions" in sqlite_import.import_table_order()
    assert sqlite_import.source_table_columns("m4_intent_decisions") == (
        "request_key",
        "resolved_task_type",
        "decision_status",
        "decision_source",
        "adapter_id",
        "adapter_version",
        "policy_version",
        "confidence",
        "margin",
        "input_checksum",
        "reason_codes_json",
        "shadow_json",
        "schema_version",
        "payload_checksum",
        "created_at",
    )


def test_intent_import_reports_empty_and_checksum_verified_rows(
    tmp_path: Path,
) -> None:
    empty_source = _source_with_intents(tmp_path / "empty.sqlite3", "")
    empty = SQLiteToPostgresMigrator(
        source_path=empty_source,
        destination=_MemoryDestination(),
    ).run(mode="dry-run")
    empty_table = next(
        table for table in empty.tables if table.table == "m4_intent_decisions"
    )
    assert empty_table.source_count == empty_table.verified_count == 0

    source = _source_with_intents(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination()
    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
    ).run(mode="apply", batch_size=1)
    intent_table = next(
        table for table in report.tables if table.table == "m4_intent_decisions"
    )
    assert intent_table.source_count == intent_table.verified_count == 2
    assert len(intent_table.checksum_digest) == 64
    assert {
        row.checksum
        for (table, _), row in destination.rows.items()
        if table == "m4_intent_decisions"
    } == {_decision("a").payload_checksum, _decision("b").payload_checksum}


def test_intent_import_checkpoint_resumes_new_instance_at_first_incomplete_batch(
    tmp_path: Path,
) -> None:
    source = _source_with_intents(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination(fail_identity=("b" * 64,))
    report_path = tmp_path / "report.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    destination_fingerprint = "d" * 64

    with pytest.raises(MigrationError, match="MIGRATION_BATCH_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint=destination_fingerprint,
        ).run(
            mode="apply",
            batch_size=1,
            report_path=report_path,
            checkpoint_path=checkpoint_path,
        )

    partial = json.loads(report_path.read_text(encoding="utf-8"))
    assert partial["completed_batches"] == 1
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "applying"
    assert checkpoint["next_batch_index"] == 1
    assert ("m4_intent_decisions", ("a" * 64,)) in destination.rows
    destination.fail_identity = None

    resumed = SQLiteToPostgresMigrator(
        source_path=source,
        destination=destination,
        destination_fingerprint=destination_fingerprint,
    ).run(
        mode="apply",
        batch_size=1,
        report_path=report_path,
        checkpoint_path=checkpoint_path,
    )

    assert resumed.status == "completed"
    assert resumed.completed_batches == 2
    assert destination.verify_calls == 1
    assert destination.apply_calls == 3
    assert destination.applied_batches[-1] == (
        "m4_intent_decisions",
        (("b" * 64,),),
    )
    completed_checkpoint = json.loads(
        checkpoint_path.read_text(encoding="utf-8")
    )
    assert completed_checkpoint["status"] == "completed"
    assert completed_checkpoint["next_batch_index"] == 2
    assert len(
        [
            key
            for key in destination.rows
            if key[0] == "m4_intent_decisions"
        ]
    ) == 2


def test_import_rejects_tampered_checkpoint_before_target_access(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination(
        fail_identity=(f"task_{'b' * 64}",)
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    with pytest.raises(MigrationError, match="MIGRATION_BATCH_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    tampered = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    tampered["next_batch_index"] = 0
    checkpoint_path.write_text(json.dumps(tampered), encoding="utf-8")
    before_apply_calls = destination.apply_calls
    before_verify_calls = destination.verify_calls

    with pytest.raises(MigrationError, match="MIGRATION_CHECKPOINT_INVALID"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    assert destination.apply_calls == before_apply_calls
    assert destination.verify_calls == before_verify_calls


def test_import_redacts_structurally_malformed_checkpoint(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination(
        fail_identity=(f"task_{'b' * 64}",)
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    with pytest.raises(MigrationError, match="MIGRATION_BATCH_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    malformed = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    malformed["status"] = ["student-private-text"]
    malformed.pop("checkpoint_checksum")
    unsigned = json.dumps(
        malformed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    malformed["checkpoint_checksum"] = hashlib.sha256(
        unsigned.encode("utf-8")
    ).hexdigest()
    checkpoint_path.write_text(json.dumps(malformed), encoding="utf-8")
    before_apply_calls = destination.apply_calls

    with pytest.raises(MigrationError, match="MIGRATION_CHECKPOINT_INVALID"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    assert destination.apply_calls == before_apply_calls


def test_import_rejects_checkpoint_for_changed_logical_source(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination(
        fail_identity=(f"task_{'b' * 64}",)
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    with pytest.raises(MigrationError, match="MIGRATION_BATCH_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    plan, key = _plan("c")
    SQLiteM4Repository(source).save_task_plan(plan, key)
    before_apply_calls = destination.apply_calls
    destination.fail_identity = None

    with pytest.raises(
        MigrationError,
        match="MIGRATION_CHECKPOINT_SOURCE_MISMATCH",
    ):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    assert destination.apply_calls == before_apply_calls


def test_import_rejects_checkpoint_for_another_destination(
    tmp_path: Path,
) -> None:
    source = _source_with_plans(tmp_path / "source.sqlite3", "ab")
    destination = _MemoryDestination(
        fail_identity=(f"task_{'b' * 64}",)
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    with pytest.raises(MigrationError, match="MIGRATION_BATCH_FAILED"):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="d" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )
    destination.fail_identity = None
    before_apply_calls = destination.apply_calls

    with pytest.raises(
        MigrationError,
        match="MIGRATION_CHECKPOINT_DESTINATION_MISMATCH",
    ):
        SQLiteToPostgresMigrator(
            source_path=source,
            destination=destination,
            destination_fingerprint="e" * 64,
        ).run(
            mode="apply",
            batch_size=1,
            checkpoint_path=checkpoint_path,
        )

    assert destination.apply_calls == before_apply_calls


def test_intent_import_rejects_tampered_checksum_before_target_write(
    tmp_path: Path,
) -> None:
    source = _source_with_intents(tmp_path / "source.sqlite3")
    connection = __import__("sqlite3").connect(source)
    try:
        connection.execute(
            "UPDATE m4_intent_decisions SET payload_checksum = ?",
            ("0" * 64,),
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
                "m4_intent_decisions",
                "m5_learner_states",
                "m5_class_states",
                "m5_state_updates",
                "m6_session_states",
                "m6_tutoring_decisions",
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


def _prepared_intent_row(
    *,
    status: IntentStatus = IntentStatus.ACCEPTED,
    integer_scores: bool = False,
) -> PreparedImportRow:
    decision = _decision(
        "a",
        status=status,
        integer_scores=integer_scores,
    )
    shadow = decision.shadow_payload()
    columns = (
        "request_key",
        "resolved_task_type",
        "decision_status",
        "decision_source",
        "adapter_id",
        "adapter_version",
        "policy_version",
        "confidence",
        "margin",
        "input_checksum",
        "reason_codes_json",
        "shadow_json",
        "schema_version",
        "payload_checksum",
        "created_at",
    )
    values = (
        decision.request_key,
        decision.resolved_task_type,
        decision.decision_status.value,
        decision.decision_source,
        decision.adapter_id,
        decision.adapter_version,
        decision.policy_version,
        decision.confidence,
        decision.margin,
        decision.input_checksum,
        json.dumps(
            list(decision.reason_codes),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        (
            None
            if shadow is None
            else json.dumps(
                shadow,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        decision.schema_version,
        decision.payload_checksum,
        decision.created_at.isoformat(),
    )
    return PreparedImportRow(
        table="m4_intent_decisions",
        columns=columns,
        values=values,
        identity=(decision.request_key,),
        version=(decision.schema_version,),
        checksum=str(decision.payload_checksum),
        fingerprint="e" * 64,
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


def test_postgres_destination_upserts_and_verifies_m4_intent_json_and_utc() -> None:
    row = _prepared_intent_row()
    selected = dict(zip(row.columns, row.values, strict=True))
    selected["reason_codes_json"] = json.loads(selected["reason_codes_json"])
    selected["shadow_json"] = json.loads(selected["shadow_json"])
    selected["created_at"] = datetime.fromisoformat(selected["created_at"])
    selected["_shadow_json_is_sql_null"] = False
    connection = _FakePostgresConnection(selected)
    destination = PostgresImportDestination(_FakePool(connection))

    destination.apply_batch("m4_intent_decisions", (row,))

    insert, parameters = next(
        execution
        for execution in connection.executed
        if execution[0].lstrip().startswith(
            "INSERT INTO m4_intent_decisions"
        )
    )
    assert "ON CONFLICT (request_key) DO NOTHING" in insert
    assert isinstance(parameters[10], Jsonb)
    assert isinstance(parameters[11], Jsonb)
    assert parameters[14].tzinfo is timezone.utc


def test_refusal_with_null_shadow_imports_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    repository = SQLiteM4Repository(source)
    repository.initialize()
    refusal = _decision("a", status=IntentStatus.INVALID)
    repository.insert_or_get_intent_decision(refusal)
    expected = _prepared_intent_row(status=IntentStatus.INVALID)
    selected = dict(zip(expected.columns, expected.values, strict=True))
    selected["reason_codes_json"] = json.loads(
        selected["reason_codes_json"]
    )
    selected["created_at"] = datetime.fromisoformat(selected["created_at"])
    selected["_shadow_json_is_sql_null"] = True
    connection = _FakePostgresConnection(selected)

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=PostgresImportDestination(_FakePool(connection)),
    ).run(mode="apply", batch_size=1)

    intent_report = next(
        table for table in report.tables if table.table == "m4_intent_decisions"
    )
    assert intent_report.source_count == intent_report.verified_count == 1
    insert_parameters = next(
        parameters
        for statement, parameters in connection.executed
        if statement.lstrip().startswith(
            "INSERT INTO m4_intent_decisions"
        )
    )
    assert insert_parameters[11] is None


def test_refusal_import_rejects_json_null_for_nullable_shadow() -> None:
    expected = _prepared_intent_row(status=IntentStatus.INVALID)
    selected = dict(zip(expected.columns, expected.values, strict=True))
    selected["reason_codes_json"] = json.loads(
        selected["reason_codes_json"]
    )
    selected["created_at"] = datetime.fromisoformat(selected["created_at"])
    selected["_shadow_json_is_sql_null"] = False
    connection = _FakePostgresConnection(selected)

    with pytest.raises(MigrationError, match="TARGET_VERIFICATION_FAILED"):
        PostgresImportDestination(_FakePool(connection)).apply_batch(
            "m4_intent_decisions",
            (expected,),
        )


def test_legacy_v1_integer_score_row_imports_without_checksum_rewrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    repository = SQLiteM4Repository(source)
    repository.initialize()
    candidate = _decision("a", integer_scores=True)
    repository.insert_or_get_intent_decision(candidate)
    legacy_checksum = _legacy_v1_integer_score_checksum(candidate)
    assert legacy_checksum != candidate.payload_checksum
    legacy_shadow = candidate.shadow_payload()
    assert legacy_shadow is not None
    legacy_shadow = {
        **legacy_shadow,
        "confidence": 1,
        "margin": 0,
    }
    legacy_shadow_json = json.dumps(
        legacy_shadow,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = __import__("sqlite3").connect(source)
    try:
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET payload_checksum = ?, shadow_json = ?
            WHERE request_key = ?
            """,
            (legacy_checksum, legacy_shadow_json, candidate.request_key),
        )
        connection.commit()
    finally:
        connection.close()
    expected = _prepared_intent_row(integer_scores=True)
    selected = dict(zip(expected.columns, expected.values, strict=True))
    selected["payload_checksum"] = legacy_checksum
    selected["reason_codes_json"] = json.loads(
        selected["reason_codes_json"]
    )
    selected["shadow_json"] = json.loads(legacy_shadow_json)
    selected["created_at"] = datetime.fromisoformat(selected["created_at"])
    selected["_shadow_json_is_sql_null"] = False
    postgres = _FakePostgresConnection(selected)

    report = SQLiteToPostgresMigrator(
        source_path=source,
        destination=PostgresImportDestination(_FakePool(postgres)),
    ).run(mode="apply", batch_size=1)

    intent_report = next(
        table for table in report.tables if table.table == "m4_intent_decisions"
    )
    assert intent_report.source_count == intent_report.verified_count == 1
    insert_parameters = next(
        parameters
        for statement, parameters in postgres.executed
        if statement.lstrip().startswith(
            "INSERT INTO m4_intent_decisions"
        )
    )
    assert insert_parameters[7:9] == (1.0, 0.0)
    assert insert_parameters[13] == legacy_checksum


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


def test_cli_destination_binding_excludes_rotatable_credentials() -> None:
    from course_insight.infrastructure.postgresql import sqlite_import_cli

    first = sqlite_import_cli._destination_fingerprint(
        "postgresql://importer:first-secret@db.internal:5432/course"
    )
    rotated = sqlite_import_cli._destination_fingerprint(
        "postgresql://importer:second-secret@db.internal:5432/course"
    )
    another_database = sqlite_import_cli._destination_fingerprint(
        "postgresql://importer:first-secret@db.internal:5432/other"
    )

    assert first == rotated
    assert first != another_database
    assert len(first) == 64


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
    checkpoint = tmp_path / "migration-checkpoint.json"
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
            "--checkpoint",
            str(checkpoint),
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
    persisted_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert persisted_checkpoint["status"] == "completed"
    assert persisted_checkpoint["next_batch_index"] == 1
    assert persisted_checkpoint["destination_fingerprint"] == (
        "338b6cc7b54e73b710144747a9d7adbe"
        "2f73f9e94f33eb31ed7a2b7d8e366e5e"
    )


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
