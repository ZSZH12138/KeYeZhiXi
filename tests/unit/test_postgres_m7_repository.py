from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresOperationError,
)
from tests.integration.test_web_workflow_persistence import (
    _audit,
    _feedback,
)
from tests.unit._postgres_repository_fakes import (
    FailingPool,
    FakeConnection,
    FakePool,
)

try:
    from course_insight.infrastructure.postgresql.m7_repository import (
        PostgresM7Repository,
    )
except ModuleNotFoundError:
    PostgresM7Repository = None  # type: ignore[assignment,misc]


def _feedback_row(package: Any) -> dict[str, Any]:
    return {
        "feedback_id": package.feedback_id,
        "task_id": package.task_id,
        "learner_id": package.learner_id,
        "payload": package.to_dict(),
        "payload_checksum": package.content_checksum(),
        "schema_version": package.schema_version,
    }


def test_postgres_m7_repository_module_exists() -> None:
    assert PostgresM7Repository is not None


@pytest.mark.skipif(
    PostgresM7Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m7_feedback_round_trips_under_both_unique_keys() -> None:
    feedback = _feedback()

    def respond(statement: str, _: tuple[Any, ...]):
        if "FROM m7_student_feedback" in statement:
            return _feedback_row(feedback)
        return None

    connection = FakeConnection(respond)
    repository = PostgresM7Repository(FakePool(connection))

    assert repository.insert_or_get_feedback(feedback) == feedback
    repository.save_feedback(feedback.model_copy(deep=True))
    assert repository.get_feedback(feedback.feedback_id) == feedback
    assert repository.get_feedback_for_task(
        feedback.task_id,
        feedback.learner_id,
    ) == feedback
    assert repository.get_feedback_by_task_and_learner(
        feedback.task_id,
        feedback.learner_id,
    ) == feedback

    repository.save_model_audit("audit_1", _audit())
    repository.save_prompt_record(
        "prompt_1",
        {"template": "governed", "version": 1},
    )

    insert_parameters = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m7_student_feedback" in statement
    )
    payload = next(
        parameter
        for parameter in insert_parameters
        if isinstance(parameter, Jsonb)
    )
    assert payload.obj == feedback.to_dict()
    assert feedback.content_checksum() in insert_parameters
    assert feedback.schema_version in insert_parameters
    assert connection.transaction_entries == 2


@pytest.mark.skipif(
    PostgresM7Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m7_rejects_unique_identity_conflicts() -> None:
    feedback = _feedback()
    conflicting = feedback.model_copy(
        update={
            "feedback_id": "feedback_2",
            "message": "Different governed content.",
        }
    )

    def respond(statement: str, _: tuple[Any, ...]):
        if "FROM m7_student_feedback" in statement:
            return _feedback_row(feedback)
        return None

    repository = PostgresM7Repository(
        FakePool(FakeConnection(respond))
    )

    with pytest.raises(RuntimeError, match="conflict"):
        repository.insert_or_get_feedback(conflicting)


@pytest.mark.skipif(
    PostgresM7Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m7_revalidates_jsonb_and_checksum_on_read() -> None:
    feedback = _feedback()
    corrupt = {
        **_feedback_row(feedback),
        "payload_checksum": "f" * 64,
    }

    def respond(statement: str, _: tuple[Any, ...]):
        return corrupt if "FROM m7_student_feedback" in statement else None

    repository = PostgresM7Repository(
        FakePool(FakeConnection(respond))
    )

    with pytest.raises(RuntimeError, match="checksum"):
        repository.get_feedback(feedback.feedback_id)


@pytest.mark.skipif(
    PostgresM7Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m7_empty_recovery_and_safe_database_errors() -> None:
    repository = PostgresM7Repository(
        FakePool(FakeConnection(lambda _statement, _parameters: None))
    )

    assert repository.get_feedback("missing") is None
    assert repository.get_feedback_for_task("missing", "learner_1") is None

    connection_error = PostgresConnectionError("database unavailable")
    failing_repository = PostgresM7Repository(FailingPool(connection_error))
    with pytest.raises(PostgresConnectionError) as captured:
        failing_repository.get_feedback("feedback_1")
    assert captured.value is connection_error

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    unsafe_repository = PostgresM7Repository(
        FakePool(FakeConnection(fail_operation))
    )
    with pytest.raises(
        PostgresOperationError,
        match="PostgreSQL repository operation failed",
    ) as operation:
        unsafe_repository.get_feedback("feedback_1")
    assert operation.value.__cause__ is None
    assert "private SQL detail" not in str(operation.value)

    insert_repository = PostgresM7Repository(
        FakePool(FakeConnection(fail_operation))
    )
    with pytest.raises(
        PostgresOperationError,
        match="PostgreSQL repository operation failed",
    ):
        insert_repository.insert_or_get_feedback(_feedback())

    task_repository = PostgresM7Repository(
        FakePool(FakeConnection(fail_operation))
    )
    with pytest.raises(
        PostgresOperationError,
        match="PostgreSQL repository operation failed",
    ):
        task_repository.get_feedback_for_task("task_1", "learner_1")
