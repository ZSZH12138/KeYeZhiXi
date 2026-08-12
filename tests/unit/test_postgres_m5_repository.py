from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresOperationError,
)
from tests.integration.test_web_workflow_persistence import _state_result
from tests.unit._postgres_repository_fakes import (
    FailingPool,
    FakeConnection,
    FakePool,
)

try:
    from course_insight.infrastructure.postgresql.m5_repository import (
        PostgresM5Repository,
    )
except ModuleNotFoundError:
    PostgresM5Repository = None  # type: ignore[assignment,misc]


def _contract_columns(contract: Any) -> dict[str, Any]:
    return {
        "payload": contract.to_dict(),
        "payload_checksum": contract.content_checksum(),
        "schema_version": contract.schema_version,
    }


def _learner_row(result: Any) -> dict[str, Any]:
    learner = result.learner_state_snapshot
    return {
        "snapshot_id": learner.snapshot_id,
        "course_id": learner.course_id,
        "class_id": learner.class_id,
        "learner_id": learner.learner_id,
        "state_version": learner.state_version,
        **_contract_columns(learner),
    }


def _class_row(result: Any, *, state_version: int) -> dict[str, Any]:
    snapshot = result.class_state_snapshot
    return {
        "snapshot_id": snapshot.snapshot_id,
        "course_id": snapshot.course_id,
        "class_id": snapshot.class_id,
        "state_version": state_version,
        "aggregation_policy_version": snapshot.aggregation_policy_version,
        **_contract_columns(snapshot),
    }


def _update_row(result: Any) -> dict[str, Any]:
    learner = result.learner_state_snapshot
    return {
        "attempt_id": result.diagnosis_result.attempt_id,
        "course_id": learner.course_id,
        "class_id": learner.class_id,
        "learner_id": learner.learner_id,
        "state_version": learner.state_version,
        **_contract_columns(result),
    }


def _observation_row(observation: Any) -> dict[str, Any]:
    return {
        "observation_id": observation.observation_id,
        "course_id": observation.course_id,
        "class_id": observation.class_id,
        "learner_id": observation.learner_id,
        "attempt_id": observation.attempt_id,
        "occurred_at": observation.occurred_at,
        **_contract_columns(observation),
    }


def _dina_model_row(model: Any) -> dict[str, Any]:
    return {
        "model_id": model.model_id,
        "course_id": model.course_id,
        "model_version": model.model_version,
        "created_at": model.created_at,
        **_contract_columns(model),
    }


def _bkt_model_row(model: Any) -> dict[str, Any]:
    return {
        "model_id": model.model_id,
        "course_id": model.course_id,
        "model_version": model.model_version,
        "created_at": model.created_at,
        **_contract_columns(model),
    }


def _knowledge_trace_row(trace: Any) -> dict[str, Any]:
    return {
        "trace_id": trace.trace_id,
        "course_id": trace.course_id,
        "class_id": trace.class_id,
        "learner_id": trace.learner_id,
        "model_version": trace.model_version,
        "updated_at": trace.updated_at,
        **_contract_columns(trace),
    }


def _responder_for(result: Any):
    def respond(statement: str, _: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "pg_advisory_xact_lock" in normalized:
            return {"locked": None}
        if "from m5_learner_states" in normalized:
            return _learner_row(result)
        if "from m5_class_states" in normalized:
            return _class_row(result, state_version=1)
        if "from m5_state_updates" in normalized:
            return _update_row(result)
        return None

    return respond


def test_postgres_m5_repository_module_exists() -> None:
    assert PostgresM5Repository is not None


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_persists_observations_and_dina_model_versions() -> None:
    """Catch PostgreSQL lagging behind SQLite model-runtime behavior."""

    from course_insight.modules.m5_learner_class_state.dina import DinaEngine
    from tests.unit.test_m5_dina import _q_matrix, _training_cohort

    cohort = _training_cohort()
    model = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=30,
    ).fit(cohort, _q_matrix())
    observations = [item for batch in cohort for item in batch.observations]
    observation_by_id = {item.observation_id: item for item in observations}

    def respond(statement: str, parameters: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m5_learning_observations" in normalized:
            if len(parameters) == 1:
                return _observation_row(observation_by_id[str(parameters[0])])
            return [_observation_row(item) for item in observations]
        if "from m5_dina_models" in normalized:
            return _dina_model_row(model)
        return None

    connection = FakeConnection(respond)
    repository = PostgresM5Repository(FakePool(connection))

    assert repository.insert_or_get_learning_observation_batch(cohort[0]) == cohort[0]
    assert repository.list_learning_observations(
        course_id="course_1",
        class_id="class_1",
    ) == observations
    assert repository.insert_or_get_dina_model(model) == model
    assert repository.get_dina_model(
        course_id="course_1",
        model_version=model.model_version,
    ) == model
    assert repository.get_latest_dina_model(
        course_id="course_1",
        class_id="class_1",
    ) == model

    inserted_payloads = [
        parameter.obj
        for statement, parameters in connection.executions
        if "INSERT INTO m5_" in statement
        for parameter in parameters
        if isinstance(parameter, Jsonb)
    ]
    assert cohort[0].observations[0].to_dict() in inserted_payloads
    assert model.to_dict() in inserted_payloads


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_persists_bkt_models_and_knowledge_traces() -> None:
    """Catch PostgreSQL dropping BKT models or restart watermarks."""

    from course_insight.modules.m5_learner_class_state.bkt import BktEngine
    from tests.unit.test_m5_bkt import _sequence

    sequences = [
        _sequence("learner_1", [True, True, False, True, True]),
        _sequence("learner_2", [False, True, True, True, False]),
        _sequence("learner_3", [False, False, True, True, True]),
        _sequence("learner_4", [True, False, False, True, False]),
    ]
    engine = BktEngine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=50,
    )
    model = engine.fit(sequences)
    trace = engine.update(model, sequences[0])

    def respond(statement: str, _: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m5_bkt_models" in normalized:
            return _bkt_model_row(model)
        if "from m5_knowledge_traces" in normalized:
            return _knowledge_trace_row(trace)
        return None

    connection = FakeConnection(respond)
    repository = PostgresM5Repository(FakePool(connection))

    assert repository.insert_or_get_bkt_model(model) == model
    assert repository.get_bkt_model(
        course_id=model.course_id,
        model_version=model.model_version,
    ) == model
    assert repository.get_latest_bkt_model(
        course_id=model.course_id,
        class_id=model.class_id,
    ) == model
    assert repository.insert_or_get_knowledge_trace(trace) == trace
    assert repository.get_knowledge_trace(trace_id=trace.trace_id) == trace

    inserted_payloads = [
        parameter.obj
        for statement, parameters in connection.executions
        if "INSERT INTO m5_" in statement
        for parameter in parameters
        if isinstance(parameter, Jsonb)
    ]
    assert model.to_dict() in inserted_payloads
    assert trace.to_dict() in inserted_payloads


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_implements_complete_protocol_and_serializes_contracts() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    connection = FakeConnection(_responder_for(result))
    repository = PostgresM5Repository(FakePool(connection))

    assert repository.insert_or_get_state_update(result) == result
    repository.save_state_update(result.model_copy(deep=True))
    repository.save_learner_state(result.learner_state_snapshot)
    repository.save_class_state(result.class_state_snapshot)
    assert repository.get_learner_state("learner_1", 1) == (
        result.learner_state_snapshot
    )
    assert repository.get_class_state(
        result.class_state_snapshot.snapshot_id
    ) == result.class_state_snapshot
    assert repository.get_state_update("attempt_1") == result
    assert repository.get_state_update_result("attempt_1") == result
    assert repository.get_state_update_version("attempt_1", 1) == result
    assert repository.get_state_update_for_audit(
        "attempt_1",
        "audit_attempt_1",
        1,
    ) == result
    assert repository.get_latest_learner_state(
        "course_1",
        "class_1",
        "learner_1",
    ) == result.learner_state_snapshot
    assert repository.list_latest_learner_states(
        "course_1",
        "class_1",
    ) == [result.learner_state_snapshot]
    assert repository.get_latest_class_state(
        "course_1",
        "class_1",
    ) == result.class_state_snapshot
    assert repository.get_latest_class_state_version(
        "course_1",
        "class_1",
    ) == 1
    assert repository.get_processed_audit_ids(
        "course_1",
        "class_1",
        "learner_1",
    ) == frozenset(result.processed_audit_ids)

    insert_parameters = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m5_state_updates" in statement
    )
    payload = next(
        parameter
        for parameter in insert_parameters
        if isinstance(parameter, Jsonb)
    )
    assert payload.obj == result.to_dict()
    assert result.content_checksum() in insert_parameters
    assert result.schema_version in insert_parameters
    assert connection.transaction_entries >= 3


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_legacy_scope_lookup_fails_closed_when_ambiguous() -> None:
    first = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="shared_class",
        state_version=1,
    )
    second = _state_result(
        attempt_id="attempt_2",
        course_id="course_2",
        class_id="shared_class",
        state_version=1,
    )

    def respond(statement: str, _: tuple[Any, ...]):
        if "FROM m5_learner_states" in statement:
            return [_learner_row(first), _learner_row(second)]
        return None

    repository = PostgresM5Repository(
        FakePool(FakeConnection(respond))
    )

    with pytest.raises(RuntimeError, match="ambiguous across courses"):
        repository.get_learner_state("learner_1", 1)


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_exact_state_reads_use_complete_scope_and_version() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=3,
    )

    def respond(statement: str, parameters: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m5_learner_states" in normalized:
            if parameters == ("course_1", "class_1", "learner_1", 3):
                return _learner_row(result)
            return None
        if "from m5_class_states" in normalized:
            if parameters == ("course_1", "class_1", 3):
                return _class_row(result, state_version=3)
            return None
        return None

    connection = FakeConnection(respond)
    repository = PostgresM5Repository(FakePool(connection))

    assert repository.get_learner_state_exact(
        "course_1",
        "class_1",
        "learner_1",
        3,
    ) == result.learner_state_snapshot
    assert repository.get_class_state_exact(
        "course_1",
        "class_1",
        3,
    ) == result.class_state_snapshot
    assert repository.get_learner_state_exact(
        "course_missing",
        "class_1",
        "learner_1",
        3,
    ) is None
    assert repository.get_class_state_exact(
        "course_missing",
        "class_1",
        3,
    ) is None

    learner_statement, learner_parameters = connection.executions[0]
    class_statement, class_parameters = connection.executions[1]
    assert "course_id = %s" in learner_statement
    assert "class_id = %s" in learner_statement
    assert "learner_id = %s" in learner_statement
    assert "state_version = %s" in learner_statement
    assert learner_parameters == ("course_1", "class_1", "learner_1", 3)
    assert "course_id = %s" in class_statement
    assert "class_id = %s" in class_statement
    assert "state_version = %s" in class_statement
    assert class_parameters == ("course_1", "class_1", 3)


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_class_identity_read_uses_complete_scope() -> None:
    first = _state_result(
        attempt_id="attempt_course_1",
        course_id="course_1",
        class_id="shared_class",
        state_version=1,
    )
    second = _state_result(
        attempt_id="attempt_course_2",
        course_id="course_2",
        class_id="shared_class",
        state_version=1,
    )
    shared_snapshot_id = "shared_class_state_v1"
    first_class = first.class_state_snapshot.model_copy(
        update={"snapshot_id": shared_snapshot_id},
        deep=True,
    )
    second_class = second.class_state_snapshot.model_copy(
        update={"snapshot_id": shared_snapshot_id},
        deep=True,
    )
    first_result = first.model_copy(
        update={"class_state_snapshot": first_class},
        deep=True,
    )
    second_result = second.model_copy(
        update={"class_state_snapshot": second_class},
        deep=True,
    )

    def respond(_: str, parameters: tuple[Any, ...]):
        if parameters == ("course_1", "shared_class", shared_snapshot_id):
            return _class_row(first_result, state_version=1)
        if parameters == ("course_2", "shared_class", shared_snapshot_id):
            return _class_row(second_result, state_version=1)
        return None

    connection = FakeConnection(respond)
    repository = PostgresM5Repository(FakePool(connection))

    assert repository.get_class_state_by_identity(
        "course_1",
        "shared_class",
        shared_snapshot_id,
    ) == first_class
    assert repository.get_class_state_by_identity(
        "course_2",
        "shared_class",
        shared_snapshot_id,
    ) == second_class
    assert repository.get_class_state_by_identity(
        "course_missing",
        "shared_class",
        shared_snapshot_id,
    ) is None
    statement, parameters = connection.executions[0]
    assert "course_id = %s" in statement
    assert "class_id = %s" in statement
    assert "snapshot_id = %s" in statement
    assert parameters == ("course_1", "shared_class", shared_snapshot_id)


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_rejects_checksum_and_insert_or_get_conflicts() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )

    def corrupt_checksum(statement: str, parameters: tuple[Any, ...]):
        response = _responder_for(result)(statement, parameters)
        if (
            isinstance(response, dict)
            and "FROM m5_state_updates" in statement
        ):
            return {**response, "payload_checksum": "0" * 64}
        return response

    repository = PostgresM5Repository(
        FakePool(FakeConnection(corrupt_checksum))
    )
    with pytest.raises(RuntimeError, match="checksum"):
        repository.get_state_update("attempt_1")

    conflicting = result.model_copy(
        update={"updated_at": result.updated_at.replace(hour=9)}
    )
    conflict_repository = PostgresM5Repository(
        FakePool(FakeConnection(_responder_for(result)))
    )
    with pytest.raises(RuntimeError, match="conflict"):
        conflict_repository.insert_or_get_state_update(conflicting)


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_allocates_first_class_version_under_advisory_lock() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    class_reads = 0

    def respond(statement: str, _: tuple[Any, ...]):
        nonlocal class_reads
        normalized = " ".join(statement.lower().split())
        if "pg_advisory_xact_lock" in normalized:
            return {"locked": None}
        if "select coalesce(max(state_version)" in normalized:
            return {"next_version": 1}
        if "from m5_class_states" in normalized:
            class_reads += 1
            if class_reads == 1:
                return None
            return _class_row(result, state_version=1)
        return None

    connection = FakeConnection(respond)
    repository = PostgresM5Repository(FakePool(connection))

    repository.save_class_state(result.class_state_snapshot)

    insert_parameters = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m5_class_states" in statement
    )
    assert 1 in insert_parameters
    assert result.class_state_snapshot.content_checksum() in insert_parameters
    assert any(
        "pg_advisory_xact_lock" in statement
        for statement, _ in connection.executions
    )


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_checks_locked_class_baseline_atomically() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    class_reads = 0

    def first_insert(statement: str, parameters: tuple[Any, ...]):
        nonlocal class_reads
        normalized = " ".join(statement.lower().split())
        if "pg_advisory_xact_lock" in normalized:
            return {"locked": None}
        if "select snapshot_id from m5_class_states" in normalized:
            return None
        if "from m5_learner_states" in normalized:
            return _learner_row(result)
        if "from m5_class_states" in normalized:
            class_reads += 1
            return _class_row(result, state_version=1)
        if "from m5_state_updates" in normalized:
            return _update_row(result)
        return None

    repository = PostgresM5Repository(
        FakePool(FakeConnection(first_insert))
    )
    assert repository.insert_or_get_state_update(
        result,
        expected_previous_class_snapshot_id=None,
    ) == result

    persisted = _responder_for(result)

    def stale_baseline(statement: str, parameters: tuple[Any, ...]):
        if "from m5_state_updates" in " ".join(statement.lower().split()):
            return None
        return persisted(statement, parameters)

    conflict_repository = PostgresM5Repository(
        FakePool(FakeConnection(stale_baseline))
    )
    with pytest.raises(PostgresOperationError, match="conflict"):
        conflict_repository.insert_or_get_state_update(
            result,
            expected_previous_class_snapshot_id="stale_snapshot",
        )


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_returns_identical_retry_before_class_baseline_check() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    connection = FakeConnection(_responder_for(result))
    repository = PostgresM5Repository(FakePool(connection))

    assert repository.insert_or_get_state_update(
        result,
        expected_previous_class_snapshot_id=None,
    ) == result
    assert not any(
        "select snapshot_id from m5_class_states" in " ".join(
            statement.lower().split()
        )
        for statement, _ in connection.executions
    )
    assert not any(
        "insert into m5_learner_states" in " ".join(statement.lower().split())
        for statement, _ in connection.executions
    )


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_rejects_invalid_latest_class_version() -> None:
    def invalid_version(statement: str, _: tuple[Any, ...]):
        if "SELECT MAX(state_version)" in statement:
            return {"state_version": 0}
        return None

    repository = PostgresM5Repository(
        FakePool(FakeConnection(invalid_version))
    )
    with pytest.raises(PostgresOperationError, match="integrity"):
        repository.get_latest_class_state_version("course_1", "class_1")


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_preserves_connection_errors_for_new_scope_reads() -> None:
    connection_error = PostgresConnectionError("database unavailable")
    repository = PostgresM5Repository(FailingPool(connection_error))

    with pytest.raises(PostgresConnectionError) as learner_error:
        repository.list_latest_learner_states("course_1", "class_1")
    assert learner_error.value is connection_error
    with pytest.raises(PostgresConnectionError) as version_error:
        repository.get_latest_class_state_version("course_1", "class_1")
    assert version_error.value is connection_error


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_empty_recovery_and_safe_database_errors() -> None:
    repository = PostgresM5Repository(
        FakePool(FakeConnection(lambda _statement, _parameters: None))
    )

    assert repository.get_state_update("missing") is None
    assert repository.get_state_update_version("missing", 1) is None
    assert repository.get_state_update_for_audit(
        "missing",
        "audit_missing",
        1,
    ) is None
    assert repository.get_learner_state("missing", 1) is None
    assert repository.get_latest_learner_state(
        "course_1",
        "class_1",
        "missing",
    ) is None
    assert repository.list_latest_learner_states(
        "course_1",
        "class_1",
    ) == []
    assert repository.get_class_state("missing") is None
    assert repository.get_latest_class_state("course_1", "class_1") is None
    assert repository.get_latest_class_state_version(
        "course_1",
        "class_1",
    ) is None
    assert repository.get_processed_audit_ids(
        "course_1",
        "class_1",
        "missing",
    ) == frozenset()

    connection_error = PostgresConnectionError("database unavailable")
    failing_repository = PostgresM5Repository(FailingPool(connection_error))
    with pytest.raises(PostgresConnectionError) as captured:
        failing_repository.get_state_update("attempt_1")
    assert captured.value is connection_error

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    unsafe_repository = PostgresM5Repository(
        FakePool(FakeConnection(fail_operation))
    )
    with pytest.raises(
        PostgresOperationError,
        match="PostgreSQL repository operation failed",
    ) as operation:
        unsafe_repository.get_state_update("attempt_1")
    assert operation.value.__cause__ is None
    assert "private SQL detail" not in str(operation.value)


@pytest.mark.skipif(
    PostgresM5Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m5_sanitizes_database_errors_across_protocol_methods() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    operations = (
        lambda repository: repository.insert_or_get_state_update(result),
        lambda repository: repository.get_state_update_version(
            "attempt_1",
            1,
        ),
        lambda repository: repository.get_state_update_for_audit(
            "attempt_1",
            "audit_attempt_1",
            1,
        ),
        lambda repository: repository.save_learner_state(
            result.learner_state_snapshot
        ),
        lambda repository: repository.get_learner_state("learner_1", 1),
        lambda repository: repository.get_latest_learner_state(
            "course_1",
            "class_1",
            "learner_1",
        ),
        lambda repository: repository.list_latest_learner_states(
            "course_1",
            "class_1",
        ),
        lambda repository: repository.save_class_state(
            result.class_state_snapshot
        ),
        lambda repository: repository.get_class_state(
            result.class_state_snapshot.snapshot_id
        ),
        lambda repository: repository.get_latest_class_state(
            "course_1",
            "class_1",
        ),
        lambda repository: repository.get_latest_class_state_version(
            "course_1",
            "class_1",
        ),
        lambda repository: repository.get_processed_audit_ids(
            "course_1",
            "class_1",
            "learner_1",
        ),
    )

    for invoke in operations:
        repository = PostgresM5Repository(
            FakePool(FakeConnection(fail_operation))
        )
        with pytest.raises(
            PostgresOperationError,
            match="PostgreSQL repository operation failed",
        ) as operation:
            invoke(repository)
        assert operation.value.__cause__ is None
        assert "private SQL detail" not in str(operation.value)
