from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresOperationError,
)
from tests.integration.test_web_workflow_persistence import (
    NOW,
    _analytics,
    _review,
)
from tests.unit._postgres_repository_fakes import (
    FailingPool,
    FakeConnection,
    FakePool,
)

try:
    from course_insight.infrastructure.postgresql.m9_repository import (
        PostgresM9Repository,
    )
except ModuleNotFoundError:
    PostgresM9Repository = None  # type: ignore[assignment,misc]


def _analytics_row(bundle: Any) -> dict[str, Any]:
    return {
        "report_id": bundle.report_id,
        "course_id": "course_1",
        "class_id": bundle.class_report.class_id,
        "generated_at": bundle.generated_at,
        "learner_ids": sorted(
            report.learner_id for report in bundle.individual_reports
        ),
        "payload": bundle.to_dict(),
        "payload_checksum": bundle.content_checksum(),
        "schema_version": bundle.schema_version,
    }


def _review_row(decision: Any) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "audit_id": decision.audit_id,
        "expected_audit_version": decision.expected_audit_version,
        "payload": decision.to_dict(),
        "payload_checksum": decision.content_checksum(),
        "schema_version": decision.schema_version,
    }


def _responder(analytics: list[Any], review: Any):
    by_report = {bundle.report_id: bundle for bundle in analytics}

    def respond(statement: str, parameters: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m9_teacher_analytics" in normalized:
            if "order by generated_at desc" in normalized:
                return [_analytics_row(bundle) for bundle in reversed(analytics)]
            return _analytics_row(by_report[str(parameters[0])])
        if "from m9_teacher_reviews" in normalized:
            return _review_row(review)
        return None

    return respond


def test_postgres_m9_repository_module_exists() -> None:
    assert PostgresM9Repository is not None


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_persists_timestamptz_scope_and_unique_reviews() -> None:
    older = _analytics(
        report_id="report_older",
        generated_at=datetime(
            2026,
            7,
            25,
            8,
            30,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    )
    newer = _analytics(
        report_id="report_newer",
        generated_at=datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc),
    )
    review = _review()
    connection = FakeConnection(_responder([older, newer], review))
    repository = PostgresM9Repository(FakePool(connection))

    assert repository.insert_or_get_analytics(
        older,
        course_id="course_1",
    ) == older
    assert repository.insert_or_get_analytics(
        newer,
        course_id="course_1",
    ) == newer
    assert repository.get_analytics("report_newer") == newer
    assert repository.get_latest_analytics(
        course_id="course_1",
        class_id="class_1",
    ) == newer
    assert repository.get_latest_analytics(
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
    ) == newer

    assert repository.insert_or_get_review_decision(review) == review
    repository.save_analytics(
        newer.model_copy(deep=True),
        course_id="course_1",
    )
    repository.save_review_decision(review.model_copy(deep=True))
    assert repository.get_review_decision(review.decision_id) == review
    with pytest.raises(ValueError, match="course scope"):
        repository.save_analytics(newer)

    analytics_insert = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m9_teacher_analytics" in statement
    )
    assert older.generated_at in analytics_insert
    json_values = [
        parameter
        for parameter in analytics_insert
        if isinstance(parameter, Jsonb)
    ]
    assert [report.learner_id for report in older.individual_reports] in [
        value.obj for value in json_values
    ]
    assert older.to_dict() in [value.obj for value in json_values]
    assert older.content_checksum() in analytics_insert
    assert older.schema_version in analytics_insert
    learner_query = next(
        statement
        for statement, _ in connection.executions
        if "FROM m9_teacher_analytics" in statement
        and "learner_ids" in statement
        and "ORDER BY generated_at DESC" in statement
        and "@>" in statement
    )
    assert "@>" in learner_query


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_rejects_review_uniqueness_conflicts() -> None:
    analytics = _analytics()
    stored = _review()
    conflicting = stored.model_copy(
        update={
            "decision_id": "decision_2",
            "teacher_comment": "Different decision.",
        }
    )
    repository = PostgresM9Repository(
        FakePool(FakeConnection(_responder([analytics], stored)))
    )

    with pytest.raises(RuntimeError, match="conflict"):
        repository.insert_or_get_review_decision(conflicting)


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_revalidates_schema_and_checksum_on_read() -> None:
    analytics = _analytics(generated_at=NOW)
    review = _review()

    def corrupt(statement: str, parameters: tuple[Any, ...]):
        response = _responder([analytics], review)(statement, parameters)
        if (
            isinstance(response, dict)
            and "FROM m9_teacher_analytics" in statement
        ):
            return {
                **response,
                "schema_version": "999.0.0",
                "payload_checksum": "0" * 64,
            }
        return response

    repository = PostgresM9Repository(
        FakePool(FakeConnection(corrupt))
    )

    with pytest.raises(RuntimeError, match="schema version|checksum"):
        repository.get_analytics(analytics.report_id)


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_validates_scope_and_empty_recovery_paths() -> None:
    analytics = _analytics()
    repository = PostgresM9Repository(
        FakePool(FakeConnection(lambda _statement, _parameters: None))
    )

    with pytest.raises(ValueError, match="course scope"):
        repository.insert_or_get_analytics(analytics, course_id=" ")
    assert repository.get_analytics("missing") is None
    assert repository.get_latest_analytics(
        course_id="course_1",
        class_id="class_1",
    ) is None
    assert repository.get_review_decision("missing") is None


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_preserves_connection_errors_and_sanitizes_sql_errors() -> None:
    connection_error = PostgresConnectionError("database unavailable")
    failing_repository = PostgresM9Repository(FailingPool(connection_error))
    with pytest.raises(PostgresConnectionError) as captured:
        failing_repository.get_analytics("report_1")
    assert captured.value is connection_error

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    unsafe_repository = PostgresM9Repository(
        FakePool(FakeConnection(fail_operation))
    )
    with pytest.raises(
        PostgresOperationError,
        match="PostgreSQL repository operation failed",
    ) as operation:
        unsafe_repository.get_analytics("report_1")
    assert operation.value.__cause__ is None
    assert "private SQL detail" not in str(operation.value)


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_sanitizes_database_errors_across_protocol_methods() -> None:
    analytics = _analytics()
    review = _review()

    def fail_operation(
        _statement: str,
        _parameters: tuple[Any, ...],
    ):
        raise psycopg.DataError("private SQL detail")

    operations = (
        lambda repository: repository.insert_or_get_analytics(
            analytics,
            course_id="course_1",
        ),
        lambda repository: repository.get_latest_analytics(
            course_id="course_1",
            class_id="class_1",
        ),
        lambda repository: repository.insert_or_get_review_decision(review),
        lambda repository: repository.get_review_decision(
            review.decision_id
        ),
    )

    for invoke in operations:
        repository = PostgresM9Repository(
            FakePool(FakeConnection(fail_operation))
        )
        with pytest.raises(
            PostgresOperationError,
            match="PostgreSQL repository operation failed",
        ) as operation:
            invoke(repository)
        assert operation.value.__cause__ is None
        assert "private SQL detail" not in str(operation.value)
