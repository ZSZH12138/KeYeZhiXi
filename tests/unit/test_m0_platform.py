from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.platform import ActorContext
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.sqlite import SCHEMA_VERSION, connect_sqlite
from course_insight.modules.m0_platform.service import M0PlatformService


NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


class _UnsafeSecretContract(ContractModel):
    api_key: str


class _ResourceReferenceContract(ContractModel):
    result_ref: str


@pytest.fixture
def service(tmp_path: Path) -> M0PlatformService:
    return M0PlatformService(
        database_path=tmp_path / "runtime" / "course_insight.sqlite3",
        runtime_dir=tmp_path / "runtime",
        config_dir=tmp_path / "config",
    )


def _event(index: int) -> LearningEvent:
    return LearningEvent(
        event_id=f"event_{index}",
        event_type="assessment_scored",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        attempt_id="attempt_1",
        payload={"score": index, "source": "M8"},
        occurred_at=NOW,
    )


def _task_plan() -> TaskPlan:
    return TaskPlan(
        task_id="task_1",
        task_type="qa",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        blueprint_id=None,
        knowledge_bundle_id="knowledge_bundle_1",
        course_package_id="course_package_1",
        workflow=["M2", "M7"],
        next_module="M2",
        created_at=NOW,
    )


def _database_path(service: M0PlatformService) -> Path:
    return service._database_path  # noqa: SLF001 - white-box persistence test


def test_initialize_creates_runtime_database_and_current_schema(
    service: M0PlatformService,
) -> None:
    service.initialize()
    service.initialize()

    database_path = _database_path(service)
    assert database_path.is_file()
    assert database_path.parent.is_dir()
    with connect_sqlite(database_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
    assert version == SCHEMA_VERSION


def test_append_learning_events_persists_events_and_outbox_atomically(
    service: M0PlatformService,
) -> None:
    service.initialize()

    acknowledgement = service.append_learning_events(
        [_event(1), _event(2), _event(3)]
    )

    assert acknowledgement.accepted_event_ids == ["event_1", "event_2", "event_3"]
    assert acknowledgement.duplicate_event_ids == []
    assert acknowledgement.failed_event_ids == []
    with connect_sqlite(_database_path(service)) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM m0_learning_events"
        ).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0]
    assert event_count == 3
    assert outbox_count == 3


def test_append_learning_events_reports_duplicate_on_second_submission(
    service: M0PlatformService,
) -> None:
    service.initialize()
    first = service.append_learning_events([_event(1)])

    second = service.append_learning_events([_event(1)])

    assert first.accepted_event_ids == ["event_1"]
    assert first.duplicate_event_ids == []
    assert second.accepted_event_ids == []
    assert second.duplicate_event_ids == ["event_1"]
    with connect_sqlite(_database_path(service)) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM m0_learning_events"
        ).fetchone()[0]
    assert event_count == 1


def test_append_learning_events_collapses_same_batch_duplicates(
    service: M0PlatformService,
) -> None:
    service.initialize()

    acknowledgement = service.append_learning_events([_event(1), _event(1)])

    assert acknowledgement.accepted_event_ids == ["event_1"]
    assert acknowledgement.duplicate_event_ids == []
    assert acknowledgement.failed_event_ids == []
    with connect_sqlite(_database_path(service)) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM m0_learning_events"
        ).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0]
    assert event_count == 1
    assert outbox_count == 1


def test_later_append_delivers_prior_outbox_once(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    service.append_learning_events([_event(1)])

    service.append_learning_events([_event(2)])

    audit_path = tmp_path / "runtime" / "audit" / "learning_events.jsonl"
    assert audit_path.read_text(encoding="utf-8").count("\n") == 1
    assert '"event_id":"event_1"' in audit_path.read_text(encoding="utf-8")
    with connect_sqlite(_database_path(service)) as connection:
        pending_ids = [
            row[0]
            for row in connection.execute(
                "SELECT event_id FROM m0_event_outbox ORDER BY event_id"
            ).fetchall()
        ]
    assert pending_ids == ["event_2"]


def test_initialize_delivers_outbox_left_by_previous_process(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    service.append_learning_events([_event(1)])
    restarted_service = M0PlatformService(
        database_path=_database_path(service),
        runtime_dir=tmp_path / "runtime",
        config_dir=tmp_path / "config",
    )

    restarted_service.initialize()

    audit_path = tmp_path / "runtime" / "audit" / "learning_events.jsonl"
    assert '"event_id":"event_1"' in audit_path.read_text(encoding="utf-8")
    with connect_sqlite(_database_path(service)) as connection:
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0]
    assert outbox_count == 0


def test_event_and_outbox_roll_back_together_on_outbox_failure(
    service: M0PlatformService,
) -> None:
    service.initialize()
    with connect_sqlite(_database_path(service)) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_m0_outbox_insert
            BEFORE INSERT ON m0_event_outbox
            BEGIN
                SELECT RAISE(ABORT, 'forced outbox failure');
            END
            """
        )

    with pytest.raises(DomainError) as captured:
        service.append_learning_events([_event(1)])

    assert captured.value.code == "EVENT_PERSIST_FAILED"
    with connect_sqlite(_database_path(service)) as connection:
        event_count = connection.execute(
            "SELECT COUNT(*) FROM m0_learning_events"
        ).fetchone()[0]
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0]
    assert event_count == 0
    assert outbox_count == 0


def test_contract_snapshot_roundtrip_preserves_task_plan_checksum(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    plan = _task_plan()
    snapshot_path = tmp_path / "runtime" / "snapshots" / "task_plan.json"

    returned_path = service.save_contract_snapshot(plan, snapshot_path)
    restored = service.load_contract_snapshot(TaskPlan, snapshot_path)

    assert returned_path == snapshot_path
    assert restored == plan
    assert restored.content_checksum() == plan.content_checksum()
    assert snapshot_path.read_text(encoding="utf-8")


def test_health_check_returns_only_safe_component_statuses(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()

    health = service.health_check()

    assert health == {"config": "ok", "database": "ok", "runtime": "ok"}
    rendered = repr(health)
    assert str(tmp_path) not in rendered
    assert "DEEPSEEK_API_KEY" not in rendered


def test_prepare_django_frontend_remains_skipped(
    service: M0PlatformService,
) -> None:
    actor = ActorContext(
        actor_id="pseudonym_teacher",
        role="teacher",
        course_ids=["course_1"],
        class_ids=["class_1"],
        issued_at=NOW,
    )

    status = service.prepare_django_frontend(actor, NOW)

    assert status.status == "skipped"
    assert status.progress == 0.0
    assert status.result_ref is None
    assert status.error_code is None
    assert status.finished_at == NOW


def test_snapshot_rejects_host_absolute_paths_in_contract_data(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    unsafe_plan = _task_plan().model_copy(
        update={"session_id": str(tmp_path.resolve())},
    )

    with pytest.raises(DomainError) as captured:
        service.save_contract_snapshot(
            unsafe_plan,
            tmp_path / "runtime" / "snapshots" / "unsafe.json",
        )

    assert captured.value.code == "CONFIG_INVALID"
    assert not (tmp_path / "runtime" / "snapshots" / "unsafe.json").exists()


def test_snapshot_rejects_posix_host_absolute_paths(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    unsafe_plan = _task_plan().model_copy(
        update={"session_id": "/home/alice/private/session.json"},
    )

    with pytest.raises(DomainError) as captured:
        service.save_contract_snapshot(
            unsafe_plan,
            tmp_path / "runtime" / "snapshots" / "posix-unsafe.json",
        )

    assert captured.value.code == "CONFIG_INVALID"
    assert not (
        tmp_path / "runtime" / "snapshots" / "posix-unsafe.json"
    ).exists()


def test_snapshot_rejects_secret_fields_without_leaking_values(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    secret_value = "not-a-real-secret"

    with pytest.raises(DomainError) as captured:
        service.save_contract_snapshot(
            _UnsafeSecretContract(api_key=secret_value),
            tmp_path / "runtime" / "snapshots" / "secret.json",
        )

    assert captured.value.code == "CONFIG_INVALID"
    assert secret_value not in str(captured.value)
    assert captured.value.details == {"contract": "_UnsafeSecretContract"}


def test_snapshot_allows_portable_resource_references(
    service: M0PlatformService,
    tmp_path: Path,
) -> None:
    service.initialize()
    snapshot_path = tmp_path / "runtime" / "snapshots" / "resource.json"

    service.save_contract_snapshot(
        _ResourceReferenceContract(result_ref="jobs/123"),
        snapshot_path,
    )

    restored = service.load_contract_snapshot(
        _ResourceReferenceContract,
        snapshot_path,
    )
    assert restored.result_ref == "jobs/123"
