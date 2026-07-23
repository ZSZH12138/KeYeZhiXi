from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.contracts.knowledge import AssessmentBlueprint, KnowledgeBundle
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.sqlite import connect_sqlite
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)


NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _repository_type() -> type[Any]:
    try:
        module = importlib.import_module(
            "course_insight.infrastructure.sqlite.m4_repository"
        )
    except ImportError as error:
        pytest.fail(f"SQLiteM4Repository is unavailable: {error}")
    return module.SQLiteM4Repository


def _blueprint(blueprint_id: str) -> AssessmentBlueprint:
    return AssessmentBlueprint(
        blueprint_id=blueprint_id,
        version="1.0.0",
        course_id="course_1",
        sections=[],
        total_score=0.0,
        duration_minutes=30,
        status="teacher_approved",
    )


def _bundle(
    *,
    knowledge_bundle_id: str = "bundle_1",
    course_package_id: str = "package_1",
    blueprint_id: str = "blueprint_stage",
) -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id=knowledge_bundle_id,
        course_package_id=course_package_id,
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[_blueprint(blueprint_id)],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def _service(database_path: Path) -> M4TaskOrchestrationService:
    repository = _repository_type()(database_path)
    repository.initialize()
    return M4TaskOrchestrationService(
        repository,
        canonical_idempotency_key,
    )


def _create(
    service: M4TaskOrchestrationService,
    *,
    student_text: str = "start stage assessment",
    task_type_hint: str = "stage_assessment",
    session_id: str = "session_1",
    knowledge_bundle: KnowledgeBundle | None = None,
) -> TaskPlan:
    return service.create_task_plan(
        student_text=student_text,
        task_type_hint=task_type_hint,
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id=session_id,
        knowledge_bundle=knowledge_bundle or _bundle(),
        learner_state_snapshot=None,
    )


def _row_count(database_path: Path) -> int:
    with connect_sqlite(database_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM m4_task_plans").fetchone()[0]
        )


def test_sqlite_repository_persists_and_restores_task_plan(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)

    plan = _create(service)
    restored = _repository_type()(database_path).get_task_plan(plan.task_id)

    assert restored == plan
    assert restored is not plan
    assert restored.content_checksum() == plan.content_checksum()
    assert _row_count(database_path) == 1


def test_replay_returns_first_task_id_timestamp_and_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)

    first = _create(service, student_text="start stage assessment")
    replay = _create(service, student_text="please begin the assessment")

    assert replay.task_id == first.task_id
    assert replay.created_at == first.created_at
    assert replay.content_checksum() == first.content_checksum()
    assert _row_count(database_path) == 1


def test_new_service_instance_reuses_persisted_task(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    first = _create(_service(database_path))

    replay = _create(_service(database_path), student_text="begin assessment again")

    assert replay == first
    assert _row_count(database_path) == 1


def test_authoritative_identity_changes_create_distinct_tasks(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)

    plans = [
        _create(service),
        _create(service, session_id="session_2"),
        _create(service, task_type_hint="practice"),
        _create(
            service,
            knowledge_bundle=_bundle(knowledge_bundle_id="bundle_2"),
        ),
        _create(
            service,
            knowledge_bundle=_bundle(course_package_id="package_2"),
        ),
        _create(
            service,
            knowledge_bundle=_bundle(blueprint_id="blueprint_other"),
        ),
    ]

    assert len({plan.task_id for plan in plans}) == len(plans)
    assert _row_count(database_path) == len(plans)


def test_insert_or_get_never_overwrites_first_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)
    first = _create(service)
    repository = _repository_type()(database_path)
    changed_candidate = first.model_copy(
        update={"created_at": first.created_at + timedelta(days=1)},
        deep=True,
    )
    key = first.task_id.removeprefix("task_")

    winner = repository.insert_or_get_task_plan(changed_candidate, key)

    assert winner == first
    assert winner.created_at != changed_candidate.created_at
    assert _row_count(database_path) == 1


def test_repository_rejects_task_id_and_idempotency_key_mismatch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    service = M4TaskOrchestrationService(repository, canonical_idempotency_key)
    candidate = _create(service).model_copy(
        update={"task_id": "task_not_derived_from_the_supplied_key"},
        deep=True,
    )

    with pytest.raises(ValueError, match="task identity"):
        repository.insert_or_get_task_plan(candidate, "b" * 64)

    assert _row_count(database_path) == 1


def test_twenty_concurrent_creates_return_one_authoritative_plan(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()

    def create_once(_: int) -> TaskPlan:
        service = M4TaskOrchestrationService(
            _repository_type()(database_path),
            canonical_idempotency_key,
        )
        return _create(service)

    with ThreadPoolExecutor(max_workers=20) as executor:
        plans = list(executor.map(create_once, range(20)))

    assert len({plan.task_id for plan in plans}) == 1
    assert len({plan.created_at for plan in plans}) == 1
    assert len({plan.content_checksum() for plan in plans}) == 1
    assert _row_count(database_path) == 1
