from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresOperationError,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from tests.integration.test_web_workflow_persistence import (
    NOW,
    _paper,
    _vector_scoring_bundle,
)
from tests.factories.m5_m8 import make_paper, make_rubric
from tests.unit._postgres_repository_fakes import (
    FailingPool,
    FakeConnection,
    FakePool,
)

try:
    from course_insight.infrastructure.postgresql.m8_repository import (
        PostgresM8Repository,
    )
except ModuleNotFoundError:
    PostgresM8Repository = None  # type: ignore[assignment,misc]


def _contract_columns(contract: Any) -> dict[str, Any]:
    return {
        "payload": contract.to_dict(),
        "payload_checksum": contract.content_checksum(),
        "schema_version": contract.schema_version,
    }


def _paper_row(paper: Any) -> dict[str, Any]:
    return {
        "paper_id": paper.paper_id,
        "task_id": paper.task_id,
        "course_id": "course_1",
        "class_id": "class_1",
        "learner_id": paper.learner_id,
        **_contract_columns(paper),
    }


def _paper_record_row(record: FrozenAssessmentRecord) -> dict[str, Any]:
    return {
        "paper_id": record.paper.paper_id,
        **_contract_columns(record),
    }


def _audit_row(record: Any) -> dict[str, Any]:
    return {
        "audit_id": record.audit_id,
        "audit_version": record.audit_version,
        "item_instance_id": record.item_instance_id,
        **_contract_columns(record),
    }


def _result_key(bundle: Any) -> str:
    latest_versions: dict[str, int] = {}
    for record in bundle.score_audit_records:
        latest_versions[record.audit_id] = max(
            record.audit_version,
            latest_versions.get(record.audit_id, 0),
        )
    return dumps_json(
        [
            {"audit_id": audit_id, "audit_version": audit_version}
            for audit_id, audit_version in sorted(latest_versions.items())
        ]
    )


def _scoring_row(bundle: Any) -> dict[str, Any]:
    return {
        "attempt_id": bundle.attempt_id,
        "result_key": _result_key(bundle),
        "paper_id": bundle.paper_id,
        "learner_id": bundle.learner_id,
        "finalized_at": bundle.finalized_at,
        **_contract_columns(bundle),
    }


def _responder(
    paper: Any,
    bundles: list[Any],
    record: FrozenAssessmentRecord | None = None,
):
    result_by_key = {_result_key(bundle): bundle for bundle in bundles}
    audit_by_key = {
        (record.audit_id, record.audit_version): record
        for bundle in bundles
        for record in bundle.score_audit_records
    }

    def respond(statement: str, parameters: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m8_frozen_assessment_records" in normalized:
            return None if record is None else _paper_record_row(record)
        if "from m8_assessment_papers" in normalized:
            if normalized.startswith("select course_id, class_id"):
                return {"course_id": "course_1", "class_id": "class_1"}
            return _paper_row(paper)
        if "from m8_score_audits" in normalized:
            audit_record = audit_by_key[
                (str(parameters[0]), int(parameters[1]))
            ]
            return _audit_row(audit_record)
        if "from m8_scoring_results" in normalized:
            if "and result_key = %s" in normalized:
                return _scoring_row(result_by_key[str(parameters[1])])
            if "limit 1" in normalized:
                return _scoring_row(bundles[-1])
            return [_scoring_row(bundle) for bundle in bundles]
        return None

    return respond


def test_postgres_m8_repository_module_exists() -> None:
    assert PostgresM8Repository is not None


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_persists_scope_audit_vectors_and_history() -> None:
    paper = _paper()
    paper_record = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )
    first = _vector_scoring_bundle(
        first_version=4,
        second_version=3,
        finalized_at=NOW + timedelta(hours=1),
    )
    second = _vector_scoring_bundle(
        first_version=5,
        second_version=1,
        finalized_at=NOW + timedelta(hours=2),
    )
    connection = FakeConnection(
        _responder(paper, [first, second], paper_record)
    )
    repository = PostgresM8Repository(FakePool(connection))

    assert repository.insert_or_get_paper(
        paper,
        course_id="course_1",
        class_id="class_1",
    ) == paper
    repository.save_paper(paper.model_copy(deep=True))
    assert repository.get_paper("paper_1") == paper
    assert repository.get_paper_execution_context("paper_1") == (
        "course_1",
        "class_1",
    )
    assert repository.insert_or_get_paper_record(paper_record) == paper_record
    assert repository.get_paper_record("paper_1") == paper_record

    audit = first.get_audit_record("audit_a")
    repository.save_score_audit(audit)
    assert repository.get_score_audit(
        audit.audit_id,
        audit.audit_version,
    ) == audit
    assert repository.insert_or_get_scoring_result(first) == first
    assert repository.insert_or_get_scoring_result(second) == second
    repository.save_scoring_result(second.model_copy(deep=True))
    assert repository.get_scoring_result("attempt_vector") == second
    assert repository.get_scoring_result_by_checksum(
        "attempt_vector",
        first.content_checksum(),
    ) == first
    assert repository.get_scoring_result_for_audit(
        "attempt_vector",
        "audit_a",
        4,
    ) == first
    assert repository.get_scoring_result_for_audit(
        "attempt_vector",
        "audit_b",
        1,
    ) == first

    insert_parameters = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m8_scoring_results" in statement
    )
    payload = next(
        parameter
        for parameter in insert_parameters
        if isinstance(parameter, Jsonb)
    )
    assert payload.obj == first.to_dict()
    assert _result_key(first) in insert_parameters
    assert first.finalized_at in insert_parameters
    assert first.content_checksum() in insert_parameters
    assert first.schema_version in insert_parameters


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_saves_paper_and_frozen_record_atomically() -> None:
    paper = _paper()
    record = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )
    connection = FakeConnection(_responder(paper, [], record))
    pool = FakePool(connection)
    repository = PostgresM8Repository(pool)

    assert repository.insert_or_get_paper_record(record) == record
    assert pool.connection_entries == 1
    assert connection.transaction_entries == 1


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_rejects_payload_checksum_and_paper_freeze_tampering() -> None:
    paper = _paper()
    bundle = _vector_scoring_bundle(
        first_version=1,
        second_version=1,
        finalized_at=NOW,
    )

    def corrupt_scoring(statement: str, parameters: tuple[Any, ...]):
        response = _responder(paper, [bundle])(statement, parameters)
        if (
            isinstance(response, list)
            and "FROM m8_scoring_results" in statement
        ):
            return [
                {**row, "payload_checksum": "0" * 64}
                for row in response
            ]
        if (
            isinstance(response, dict)
            and "FROM m8_scoring_results" in statement
        ):
            return {**response, "payload_checksum": "0" * 64}
        return response

    repository = PostgresM8Repository(
        FakePool(FakeConnection(corrupt_scoring))
    )
    with pytest.raises(RuntimeError, match="checksum"):
        repository.get_scoring_result("attempt_vector")

    tampered = paper.model_copy(update={"immutable_checksum": "tampered"})

    def corrupt_paper(statement: str, _: tuple[Any, ...]):
        if "FROM m8_assessment_papers" in statement:
            return _paper_row(tampered)
        return None

    tampered_repository = PostgresM8Repository(
        FakePool(FakeConnection(corrupt_paper))
    )
    with pytest.raises(RuntimeError, match="immutable checksum"):
        tampered_repository.get_paper("paper_1")


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_insert_or_get_rejects_same_vector_different_payload() -> None:
    paper = _paper()
    stored = _vector_scoring_bundle(
        first_version=1,
        second_version=1,
        finalized_at=NOW,
    )
    conflicting = stored.model_copy(
        update={"finalized_at": NOW + timedelta(minutes=1)}
    )
    repository = PostgresM8Repository(
        FakePool(FakeConnection(_responder(paper, [stored])))
    )

    with pytest.raises(RuntimeError, match="conflict"):
        repository.insert_or_get_scoring_result(conflicting)


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_rejects_paper_and_frozen_record_conflicts() -> None:
    paper = _paper()
    changed_payload = {
        **paper.model_dump(mode="python"),
        "generated_at": paper.generated_at + timedelta(minutes=1),
        "immutable_checksum": "pending",
    }
    unfrozen = type(paper)(**changed_payload)
    changed_paper = type(paper)(
        **{**changed_payload, "immutable_checksum": unfrozen.freeze()}
    )
    paper_repository = PostgresM8Repository(
        FakePool(FakeConnection(_responder(paper, [])))
    )
    with pytest.raises(PostgresOperationError, match="conflict"):
        paper_repository.insert_or_get_paper(
            changed_paper,
            course_id="course_1",
            class_id="class_1",
        )

    subjective_paper = make_paper(subjective=True)
    original = FrozenAssessmentRecord(
        paper=subjective_paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric(version="1.0.0")],
    )
    changed_record = FrozenAssessmentRecord(
        paper=subjective_paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric(version="2.0.0")],
    )
    record_repository = PostgresM8Repository(
        FakePool(
            FakeConnection(_responder(subjective_paper, [], original))
        )
    )
    with pytest.raises(PostgresOperationError, match="conflict"):
        record_repository.insert_or_get_paper_record(changed_record)


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_rejects_mismatched_frozen_record_rows() -> None:
    paper = _paper()
    record = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )

    requested_mismatch = PostgresM8Repository(
        FakePool(FakeConnection(_responder(paper, [], record)))
    )
    with pytest.raises(PostgresOperationError, match="integrity"):
        requested_mismatch.get_paper_record("other_paper")

    def missing_paper(statement: str, _: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m8_frozen_assessment_records" in normalized:
            return _paper_record_row(record)
        return None

    with pytest.raises(PostgresOperationError, match="integrity"):
        PostgresM8Repository(
            FakePool(FakeConnection(missing_paper))
        ).get_paper_record(paper.paper_id)

    def invalid_record_identity(statement: str, _: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m8_frozen_assessment_records" in normalized:
            return {**_paper_record_row(record), "paper_id": "wrong_paper"}
        return _paper_row(paper)

    with pytest.raises(PostgresOperationError, match="integrity"):
        PostgresM8Repository(
            FakePool(FakeConnection(invalid_record_identity))
        ).get_paper_record(paper.paper_id)


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_validates_scope_and_empty_recovery_paths() -> None:
    paper = _paper()
    repository = PostgresM8Repository(
        FakePool(FakeConnection(lambda _statement, _parameters: None))
    )

    with pytest.raises(ValueError, match="execution scope"):
        repository.insert_or_get_paper(
            paper,
            course_id=" ",
            class_id="class_1",
        )
    tampered = paper.model_copy(update={"immutable_checksum": "tampered"})
    with pytest.raises(ValueError, match="immutable checksum"):
        repository.insert_or_get_paper(
            tampered,
            course_id="course_1",
            class_id="class_1",
        )
    with pytest.raises(ValueError, match="requires course and class"):
        repository.save_paper(paper)

    assert repository.get_paper("missing") is None
    assert repository.get_paper_record("missing") is None
    assert repository.get_paper_execution_context("missing") is None
    assert repository.get_score_audit("missing", 1) is None
    assert repository.get_scoring_result("missing") is None
    assert repository.get_scoring_result_by_checksum(
        "missing",
        "0" * 64,
    ) is None
    assert repository.get_scoring_result_for_audit(
        "missing",
        "audit_missing",
        1,
    ) is None


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_preserves_connection_errors_and_sanitizes_sql_errors() -> None:
    connection_error = PostgresConnectionError("database unavailable")
    failing_repository = PostgresM8Repository(FailingPool(connection_error))
    with pytest.raises(PostgresConnectionError) as captured:
        failing_repository.get_paper("paper_1")
    assert captured.value is connection_error
    record = FrozenAssessmentRecord(
        paper=_paper(),
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )
    with pytest.raises(PostgresConnectionError) as record_insert_error:
        failing_repository.insert_or_get_paper_record(record)
    assert record_insert_error.value is connection_error
    with pytest.raises(PostgresConnectionError) as record_read_error:
        failing_repository.get_paper_record("paper_1")
    assert record_read_error.value is connection_error

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    unsafe_repository = PostgresM8Repository(
        FakePool(FakeConnection(fail_operation))
    )
    with pytest.raises(
        PostgresOperationError,
        match="PostgreSQL repository operation failed",
    ) as operation:
        unsafe_repository.get_paper("paper_1")
    assert operation.value.__cause__ is None
    assert "private SQL detail" not in str(operation.value)


@pytest.mark.skipif(
    PostgresM8Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m8_sanitizes_database_errors_across_protocol_methods() -> None:
    paper = _paper()
    bundle = _vector_scoring_bundle(
        first_version=1,
        second_version=1,
        finalized_at=NOW,
    )
    audit = bundle.score_audit_records[0]

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    operations = (
        lambda repository: repository.insert_or_get_paper(
            paper,
            course_id="course_1",
            class_id="class_1",
        ),
        lambda repository: repository.get_paper_execution_context("paper_1"),
        lambda repository: repository.insert_or_get_paper_record(
            FrozenAssessmentRecord(
                paper=paper,
                course_id="course_1",
                class_id="class_1",
                frozen_rubrics=[],
            )
        ),
        lambda repository: repository.get_paper_record("paper_1"),
        lambda repository: repository.save_score_audit(audit),
        lambda repository: repository.get_score_audit(
            audit.audit_id,
            audit.audit_version,
        ),
        lambda repository: repository.insert_or_get_scoring_result(bundle),
        lambda repository: repository.get_scoring_result(bundle.attempt_id),
        lambda repository: repository.get_scoring_result_by_checksum(
            bundle.attempt_id,
            bundle.content_checksum(),
        ),
    )

    for invoke in operations:
        repository = PostgresM8Repository(
            FakePool(FakeConnection(fail_operation))
        )
        with pytest.raises(
            PostgresOperationError,
            match="PostgreSQL repository operation failed",
        ) as operation:
            invoke(repository)
        assert operation.value.__cause__ is None
        assert "private SQL detail" not in str(operation.value)
