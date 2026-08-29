from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path


from course_insight.contracts.events import LearningEvent
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.postgresql.m4_repository import (
    PostgresM4Repository,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    run_migrations,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.infrastructure.postgresql.sqlite_import import (
    PostgresImportDestination,
    SQLiteToPostgresMigrator,
)
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.modules.m0_platform.workflow import AssessmentRun
from course_insight.modules.m4_task_orchestration.intent import IntentStatus
from course_insight.modules.m4_task_orchestration.intent_service import (
    StoredIntentDecision,
)
from tests.integration._postgres_live import require_live_test_database_url


def test_real_postgres_import_is_verified_idempotent_and_source_preserving(
    tmp_path: Path,
) -> None:
    database_url = require_live_test_database_url()
    key = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    plan = TaskPlan(
        task_id=f"task_{key}",
        task_type="stage_assessment",
        course_id="migration_test_course",
        class_id="migration_test_class",
        learner_id="migration_test_learner",
        session_id=f"migration_session_{key}",
        blueprint_id="migration_blueprint",
        knowledge_bundle_id="migration_bundle",
        course_package_id="migration_package",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc),
    )
    source = tmp_path / "source.sqlite3"
    sqlite_repository = SQLiteM4Repository(source)
    sqlite_repository.initialize()
    sqlite_repository.save_task_plan(plan, key)
    intent = StoredIntentDecision(
        request_key=hashlib.sha256(f"intent:{key}".encode()).hexdigest(),
        resolved_task_type=None,
        decision_status=IntentStatus.INVALID,
        decision_source="refusal",
        adapter_id="import-adapter",
        adapter_version="v1",
        policy_version="intent-policy-v1",
        confidence=None,
        margin=None,
        input_checksum=hashlib.sha256(b"private-live-input").hexdigest(),
        reason_codes=("unsupported_hint",),
        created_at=datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc),
        _generate_checksum=True,
    )
    sqlite_repository.insert_or_get_intent_decision(intent)
    source_checksum = hashlib.sha256(source.read_bytes()).hexdigest()

    pool = PostgresPool(database_url, min_size=1, max_size=2)
    try:
        run_migrations(pool)
        with pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM m4_task_plans WHERE task_id = %s",
                    (plan.task_id,),
                )
                connection.execute(
                    "DELETE FROM m4_intent_decisions WHERE request_key = %s",
                    (intent.request_key,),
                )
        migrator = SQLiteToPostgresMigrator(
            source_path=source,
            destination=PostgresImportDestination(pool),
        )
        first = migrator.run(mode="apply", batch_size=2)
        second = migrator.run(mode="apply", batch_size=2)

        assert first.status == second.status == "completed"
        assert PostgresM4Repository(pool).get_task_plan(plan.task_id) == plan
        assert PostgresM4Repository(pool).get_intent_decision(
            intent.request_key
        ) == intent
        with pool.connection() as connection:
            stored_intent = connection.execute(
                """
                SELECT
                    shadow_json,
                    shadow_json IS NULL AS shadow_is_sql_null
                FROM m4_intent_decisions
                WHERE request_key = %s
                """,
                (intent.request_key,),
            ).fetchone()
        assert stored_intent == {
            "shadow_json": None,
            "shadow_is_sql_null": True,
        }
        intent_report = next(
            table
            for table in first.tables
            if table.table == "m4_intent_decisions"
        )
        assert intent_report.source_count == intent_report.verified_count == 1
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_checksum
    finally:
        try:
            with pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "DELETE FROM m4_task_plans WHERE task_id = %s",
                        (plan.task_id,),
                    )
                    connection.execute(
                        "DELETE FROM m4_intent_decisions WHERE request_key = %s",
                        (intent.request_key,),
                    )
        finally:
            pool.close()


def test_real_postgres_import_preserves_outbox_and_assessment_workflow(
    tmp_path: Path,
) -> None:
    database_url = require_live_test_database_url()
    source = tmp_path / "source.sqlite3"
    sqlite_repository = SQLiteM0Repository(source, outbox_clock=lambda: datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc))
    sqlite_repository.initialize()
    event_time = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
    event = LearningEvent(
        event_id="live-import-event-1",
        event_type="assessment_submitted",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        attempt_id="attempt_1",
        payload={"score": 1},
        occurred_at=event_time,
    )
    sqlite_repository.append_events((event,))
    claimed = sqlite_repository.claim_outbox_batch(
        "worker-1",
        now=event_time,
        lease_until=event_time.replace(second=30),
        batch_size=1,
    )
    assert len(claimed) == 1
    run = AssessmentRun(
        operation_id="live-operation-1",
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
        created_at=event_time,
        updated_at=event_time,
    )
    sqlite_repository.insert_or_get_assessment_run(run)

    pool = PostgresPool(database_url, min_size=1, max_size=2)
    try:
        run_migrations(pool)
        with pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM m0_event_outbox WHERE event_id = %s",
                    (event.event_id,),
                )
                connection.execute(
                    "DELETE FROM m0_learning_events WHERE event_id = %s",
                    (event.event_id,),
                )
                connection.execute(
                    "DELETE FROM m0_assessment_runs WHERE operation_id = %s",
                    (run.operation_id,),
                )
        migrator = SQLiteToPostgresMigrator(
            source_path=source,
            destination=PostgresImportDestination(pool),
        )
        report = migrator.run(mode="apply", batch_size=10)
        assert report.status == "completed"
        with pool.connection() as connection:
            outbox_row = connection.execute(
                """
                SELECT status, attempt_count, version, locked_by
                FROM m0_event_outbox
                WHERE event_id = %s
                """,
                (event.event_id,),
            ).fetchone()
            workflow_row = connection.execute(
                """
                SELECT checkpoint, status, version
                FROM m0_assessment_runs
                WHERE operation_id = %s
                """,
                (run.operation_id,),
            ).fetchone()
        assert outbox_row == {
            "status": "processing",
            "attempt_count": 1,
            "version": claimed[0].version,
            "locked_by": "worker-1",
        }
        assert workflow_row == {
            "checkpoint": "pending",
            "status": "pending",
            "version": 1,
        }
    finally:
        try:
            with pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "DELETE FROM m0_event_outbox WHERE event_id = %s",
                        (event.event_id,),
                    )
                    connection.execute(
                        "DELETE FROM m0_learning_events WHERE event_id = %s",
                        (event.event_id,),
                    )
                    connection.execute(
                        "DELETE FROM m0_assessment_runs WHERE operation_id = %s",
                        (run.operation_id,),
                    )
        finally:
            pool.close()
