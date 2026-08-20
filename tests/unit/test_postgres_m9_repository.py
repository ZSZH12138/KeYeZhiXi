from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
    _pending_rescore_analytics,
    _review,
)
from tests.unit._postgres_repository_fakes import (
    FailingPool,
    FakeConnection,
    FakePool,
)
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
    M9ReviewDecisionConflict,
)

try:
    from course_insight.infrastructure.postgresql.m9_repository import (
        PostgresM9Repository,
    )
except ModuleNotFoundError:
    PostgresM9Repository = None  # type: ignore[assignment,misc]


def _analytics_row(
    bundle: Any,
    *,
    learner_scope_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "report_id": bundle.report_id,
        "course_id": "course_1",
        "class_id": bundle.class_report.class_id,
        "generated_at": bundle.generated_at,
        "learner_ids": (
            sorted(report.learner_id for report in bundle.individual_reports)
            if learner_scope_ids is None
            else learner_scope_ids
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


def _model_audit(
    analytics: Any,
) -> M9ModelAuditRecord:
    validated_output = {"scope": "class_aggregate"}
    output_checksum = hashlib.sha256(
        json.dumps(
            validated_output,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return M9ModelAuditRecord(
        invocation_id="invocation_request_1",
        request_id="request_1",
        source_report_id=analytics.report_id,
        source_report_checksum=analytics.content_checksum(),
        scope="class_aggregate",
        prompt_template_id="m9-teacher-interpretation-json",
        prompt_template_version="3.0.0",
        output_schema_version="m9_teacher_interpretation_v2",
        policy_version="m9-teacher-interpretation-v2",
        input_checksum="a" * 64,
        source_digest="b" * 64,
        provider="deepseek",
        model_name="deepseek-v4-flash",
        provider_status="succeeded",
        validation_status="passed",
        safety_flags=(),
        input_tokens=80,
        output_tokens=40,
        latency_ms=20,
        error_code=None,
        output_checksum=output_checksum,
        validated_output=validated_output,
        created_at=NOW,
    )


def _model_audit_row(record: M9ModelAuditRecord) -> dict[str, Any]:
    payload = record.to_dict()
    return {
        "invocation_id": record.invocation_id,
        "request_id": record.request_id,
        "source_report_id": record.source_report_id,
        "source_report_checksum": record.source_report_checksum,
        "scope": record.scope,
        "provider": record.provider,
        "model_name": record.model_name,
        "provider_status": record.provider_status,
        "validation_status": record.validation_status,
        "created_at": record.created_at,
        "payload": payload,
        "payload_checksum": hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }


def _responder(
    analytics: list[Any],
    review: Any,
    model_audit: M9ModelAuditRecord | None = None,
):
    by_report = {bundle.report_id: bundle for bundle in analytics}

    def respond(statement: str, parameters: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m9_teacher_analytics" in normalized:
            if "order by generated_at desc" in normalized:
                return [_analytics_row(bundle) for bundle in reversed(analytics)]
            if (
                "and course_id = %s" in normalized
                and (
                    str(parameters[1]) != "course_1"
                    or str(parameters[2])
                    != by_report[str(parameters[0])].class_report.class_id
                )
            ):
                return None
            return _analytics_row(by_report[str(parameters[0])])
        if "from m9_teacher_reviews" in normalized:
            return _review_row(review)
        if "from m9_model_invocation_audits" in normalized:
            return (
                None
                if model_audit is None
                else _model_audit_row(model_audit)
            )
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
    assert repository.get_scoped_analytics(
        "report_newer",
        course_id="course_1",
        class_id="class_1",
    ) == newer
    assert repository.get_scoped_analytics(
        "report_newer",
        course_id="another_course",
        class_id="class_1",
    ) is None
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
    )
    assert "learner_ids @>" not in learner_query


def test_postgres_m9_latest_learner_report_uses_rejection_tombstone() -> None:
    provisional = _analytics()
    rejected = _pending_rescore_analytics()
    rejected_row = _analytics_row(
        rejected,
        learner_scope_ids=["learner_1"],
    )

    def respond(statement: str, _parameters: tuple[Any, ...]):
        if "FROM m9_teacher_analytics" in statement:
            return rejected_row
        return None

    connection = FakeConnection(respond)
    repository = PostgresM9Repository(FakePool(connection))

    assert repository.insert_or_get_analytics(
        rejected,
        course_id="course_1",
        learner_scope_ids=["learner_1"],
    ) == rejected
    assert repository.get_latest_analytics(
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
    ) == rejected

    analytics_insert = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m9_teacher_analytics" in statement
    )
    assert ["learner_1"] in [
        value.obj
        for value in analytics_insert
        if isinstance(value, Jsonb)
    ]
    latest_statement, latest_parameters = next(
        (statement, parameters)
        for statement, parameters in connection.executions
        if "ORDER BY generated_at DESC" in statement
    )
    assert "learner_ids @>" not in latest_statement
    assert latest_parameters == ("course_1", "class_1")

    corrupt_rejected_row = {
        **rejected_row,
        "learner_ids": [],
    }
    corrupt_repository = PostgresM9Repository(
        FakePool(
            FakeConnection(
                lambda statement, _parameters: (
                    [corrupt_rejected_row, _analytics_row(provisional)]
                    if "ORDER BY generated_at DESC" in statement
                    else None
                )
            )
        )
    )
    with pytest.raises(PostgresOperationError, match="integrity check"):
        corrupt_repository.get_latest_analytics(
            course_id="course_1",
            class_id="class_1",
            learner_id="learner_1",
        )


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_persists_idempotent_model_audit_without_raw_prompt() -> None:
    analytics = _analytics()
    review = _review()
    audit = _model_audit(analytics)
    connection = FakeConnection(
        _responder([analytics], review, audit)
    )
    repository = PostgresM9Repository(FakePool(connection))

    repository.save_model_audit(audit)
    assert repository.get_model_audit(audit.invocation_id) == audit

    audit_insert = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m9_model_invocation_audits" in statement
    )
    json_payloads = [
        parameter.obj
        for parameter in audit_insert
        if isinstance(parameter, Jsonb)
    ]
    assert json_payloads == [audit.to_dict()]
    serialized = json.dumps(json_payloads, ensure_ascii=False)
    assert "messages" not in serialized
    assert "api_key" not in serialized
    assert audit.source_report_checksum in serialized
    assert audit.source_report_checksum in audit_insert


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_rejects_model_audit_source_checksum_mismatch() -> None:
    analytics = _analytics()
    review = _review()
    audit = _model_audit(analytics)
    repository = PostgresM9Repository(
        FakePool(FakeConnection(_responder([analytics], review, audit)))
    )

    with pytest.raises(PostgresOperationError, match="checksum"):
        repository.save_model_audit(
            replace(audit, source_report_checksum="0" * 64)
        )


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_rejects_tampered_model_audit_source_binding() -> None:
    analytics = _analytics()
    review = _review()
    audit = _model_audit(analytics)
    base_responder = _responder([analytics], review, audit)

    def tampered(statement: str, parameters: tuple[Any, ...]):
        response = base_responder(statement, parameters)
        if (
            isinstance(response, dict)
            and "FROM m9_model_invocation_audits" in statement
        ):
            return {**response, "source_report_checksum": "0" * 64}
        return response

    repository = PostgresM9Repository(
        FakePool(FakeConnection(tampered))
    )

    with pytest.raises(PostgresOperationError, match="integrity"):
        repository.get_model_audit(audit.invocation_id)


@pytest.mark.skipif(
    PostgresM9Repository is None,
    reason="adapter is the RED-phase missing feature",
)
def test_postgres_m9_recomputes_authoritative_report_checksum() -> None:
    analytics = _analytics()
    review = _review()
    audit = _model_audit(analytics)
    base_responder = _responder([analytics], review, audit)
    tampered_bundle = analytics.model_copy(
        update={
            "class_report": analytics.class_report.model_copy(
                update={"score_statistics": {"mean": 0.5}},
                deep=True,
            )
        },
        deep=True,
    )

    def tampered_report(statement: str, parameters: tuple[Any, ...]):
        response = base_responder(statement, parameters)
        if (
            isinstance(response, dict)
            and "FROM m9_teacher_analytics" in statement
        ):
            return {**response, "payload": tampered_bundle.to_dict()}
        return response

    repository = PostgresM9Repository(
        FakePool(FakeConnection(tampered_report))
    )

    with pytest.raises(PostgresOperationError, match="checksum"):
        repository.get_model_audit(audit.invocation_id)


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

    with pytest.raises(M9ReviewDecisionConflict) as raised:
        repository.insert_or_get_review_decision(conflicting)
    assert raised.value.audit_id == conflicting.audit_id
    assert raised.value.expected_audit_version == 1


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
    assert repository.get_scoped_analytics(
        "missing",
        course_id="course_1",
        class_id="class_1",
    ) is None
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
        lambda repository: repository.get_scoped_analytics(
            analytics.report_id,
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
