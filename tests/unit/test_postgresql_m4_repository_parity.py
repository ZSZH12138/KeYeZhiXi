from __future__ import annotations

import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from types import ModuleType
from typing import Any, Iterator

import pytest
from psycopg.types.json import Jsonb

from course_insight.contracts.tasking import TaskPlan


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


def _repository_module() -> Any:
    try:
        return importlib.import_module(
            "course_insight.infrastructure.postgresql.m4_repository"
        )
    except ImportError as error:
        pytest.fail(f"PostgresM4Repository is unavailable: {error}")


def _plan(*, created_at: datetime = NOW) -> TaskPlan:
    return TaskPlan(
        task_id=f"task_{'a' * 64}",
        task_type="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        blueprint_id="blueprint_1",
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=created_at,
    )


class _Result:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self._row = deepcopy(row)

    def fetchone(self) -> dict[str, Any] | None:
        return deepcopy(self._row)


class _FakeM4Database:
    def __init__(self) -> None:
        self.lock = RLock()
        self.rows_by_key: dict[str, dict[str, Any]] = {}
        self.connection_count = 0
        self.transaction_count = 0
        self.jsonb_bind_count = 0
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_authoritative_read = False


class _Transaction:
    def __init__(self, database: _FakeM4Database) -> None:
        self._database = database
        self._backup: dict[str, dict[str, Any]] | None = None

    def __enter__(self) -> None:
        self._database.lock.acquire()
        self._database.transaction_count += 1
        self._backup = deepcopy(self._database.rows_by_key)

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: Any,
    ) -> None:
        if error_type is not None and self._backup is not None:
            self._database.rows_by_key = self._backup
        self._database.lock.release()


class _FakeConnection:
    def __init__(self, database: _FakeM4Database) -> None:
        self._database = database

    def transaction(self) -> _Transaction:
        return _Transaction(self._database)

    def execute(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> _Result:
        normalized = " ".join(query.split())
        self._database.executed.append((normalized, parameters))
        if normalized.startswith("INSERT INTO m4_task_plans"):
            (
                task_id,
                idempotency_key,
                payload,
                payload_checksum,
                schema_version,
            ) = parameters
            assert isinstance(payload, Jsonb)
            self._database.jsonb_bind_count += 1
            self._database.rows_by_key.setdefault(
                str(idempotency_key),
                {
                    "task_id": str(task_id),
                    "idempotency_key": str(idempotency_key),
                    "payload": deepcopy(payload.obj),
                    "payload_checksum": str(payload_checksum),
                    "schema_version": str(schema_version),
                },
            )
            return _Result()
        if (
            "FROM m4_task_plans" in normalized
            and "WHERE idempotency_key = %s" in normalized
        ):
            if self._database.fail_authoritative_read:
                raise RuntimeError("forced authoritative read failure")
            return _Result(self._database.rows_by_key.get(str(parameters[0])))
        if (
            "FROM m4_task_plans" in normalized
            and "WHERE task_id = %s" in normalized
        ):
            task_id = str(parameters[0])
            return _Result(
                next(
                    (
                        row
                        for row in self._database.rows_by_key.values()
                        if row["task_id"] == task_id
                    ),
                    None,
                )
            )
        raise AssertionError(f"unexpected SQL: {normalized}")


class _FakePool:
    def __init__(self, database: _FakeM4Database) -> None:
        self.database = database

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        self.database.connection_count += 1
        yield _FakeConnection(self.database)


def test_initialize_delegates_to_the_shared_migration_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _repository_module()
    database = _FakeM4Database()
    pool = _FakePool(database)
    calls: list[Any] = []
    migration_module = ModuleType(
        "course_insight.infrastructure.postgresql.migration_runner"
    )
    migration_module.run_migrations = calls.append  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        migration_module.__name__,
        migration_module,
    )

    module.PostgresM4Repository(pool).initialize()

    assert calls == [pool]


def test_insert_get_and_replay_preserve_first_authoritative_jsonb_payload() -> None:
    module = _repository_module()
    database = _FakeM4Database()
    repository = module.PostgresM4Repository(_FakePool(database))
    first = _plan()
    changed_timestamp = _plan(created_at=NOW + timedelta(days=1))

    winner = repository.insert_or_get_task_plan(first, "a" * 64)
    replay = repository.insert_or_get_task_plan(changed_timestamp, "a" * 64)
    restored = repository.get_task_plan(first.task_id)

    assert winner == first
    assert replay == first
    assert restored == first
    assert winner is not first
    assert restored is not winner
    assert restored.content_checksum() == first.content_checksum()
    assert len(database.rows_by_key) == 1
    assert database.jsonb_bind_count == 2
    assert database.transaction_count == 2
    assert database.rows_by_key["a" * 64]["payload_checksum"] == (
        first.content_checksum()
    )
    assert database.rows_by_key["a" * 64]["schema_version"] == (
        first.schema_version
    )
    assert all(
        "%s" in query
        for query, _ in database.executed
        if "WHERE" in query or query.startswith("INSERT")
    )


def test_rejects_invalid_storage_identity_before_opening_connection() -> None:
    module = _repository_module()
    database = _FakeM4Database()
    repository = module.PostgresM4Repository(_FakePool(database))
    mismatched = _plan().model_copy(update={"task_id": "task_other"}, deep=True)

    with pytest.raises(ValueError, match="identity"):
        repository.insert_or_get_task_plan(mismatched, "a" * 64)

    with pytest.raises(ValueError, match="SHA-256"):
        repository.insert_or_get_task_plan(_plan(), "not-a-digest")

    assert database.connection_count == 0


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(
            lambda row: row["payload"].update({"task_id": "task_other"}),
            id="payload-column-identity",
        ),
        pytest.param(
            lambda row: row.update({"schema_version": "99.0.0"}),
            id="stored-schema-version",
        ),
        pytest.param(
            lambda row: row["payload"].update({"schema_version": "99.0.0"}),
            id="payload-schema-version",
        ),
        pytest.param(
            lambda row: row.update({"payload_checksum": "0" * 64}),
            id="payload-checksum",
        ),
        pytest.param(
            lambda row: row["payload"].update({"workflow": ["M8", "M6"]}),
            id="business-rules",
        ),
    ],
)
def test_read_fails_closed_when_persisted_payload_is_corrupt(tamper: Any) -> None:
    module = _repository_module()
    database = _FakeM4Database()
    repository = module.PostgresM4Repository(_FakePool(database))
    repository.insert_or_get_task_plan(_plan(), "a" * 64)
    tamper(database.rows_by_key["a" * 64])

    with pytest.raises(module.PostgresOperationError, match="integrity"):
        repository.get_task_plan(_plan().task_id)


def test_transaction_rolls_back_when_authoritative_read_fails() -> None:
    module = _repository_module()
    database = _FakeM4Database()
    database.fail_authoritative_read = True
    repository = module.PostgresM4Repository(_FakePool(database))

    with pytest.raises(RuntimeError, match="forced authoritative read"):
        repository.insert_or_get_task_plan(_plan(), "a" * 64)

    assert database.rows_by_key == {}


def test_twenty_concurrent_replays_return_one_authoritative_plan() -> None:
    module = _repository_module()
    database = _FakeM4Database()
    repository = module.PostgresM4Repository(_FakePool(database))

    def insert(index: int) -> TaskPlan:
        return repository.insert_or_get_task_plan(
            _plan(created_at=NOW + timedelta(seconds=index)),
            "a" * 64,
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        plans = list(executor.map(insert, range(20)))

    assert len({plan.content_checksum() for plan in plans}) == 1
    assert len({plan.created_at for plan in plans}) == 1
    assert len(database.rows_by_key) == 1
