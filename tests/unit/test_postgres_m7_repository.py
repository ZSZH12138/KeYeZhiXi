from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from course_insight.contracts.tutoring import (
    STUDENT_CITATION_QUOTE_PLACEHOLDER,
)
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
from course_insight.modules.m7_local_model.repository import M7ModelAuditRecord


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)

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


def _feedback_payload_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _model_audit() -> M7ModelAuditRecord:
    return M7ModelAuditRecord(
        invocation_id="invocation_request_1",
        request_id="request_1",
        scoring_task_id="scoring_1",
        provider="deepseek",
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        provider_status="succeeded",
        validation_status="passed",
        prompt_template_id="m7-rubric-scoring-json",
        prompt_template_version="4.0.0",
        execution_policy_version="m7-governed-v1",
        privacy_policy_version="m7-outbound-privacy-v1",
        privacy_decision="redacted",
        student_answer_checksum="a" * 64,
        outbound_answer_checksum="b" * 64,
        prompt_input_checksum="c" * 64,
        result_checksum="d" * 64,
        evidence_ids=("evidence_1",),
        safety_flags=(
            "teacher_review_required",
            "outbound_privacy_redacted",
            "pii_email_redacted",
        ),
        safety_checked_at=NOW,
        redaction_count=1,
        input_tokens=100,
        output_tokens=50,
        latency_ms=25,
        error_code=None,
        created_at=NOW,
    )


def _model_audit_row(record: M7ModelAuditRecord) -> dict[str, Any]:
    payload = record.to_dict()
    checksum = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "invocation_id": record.invocation_id,
        "request_id": record.request_id,
        "scoring_task_id": record.scoring_task_id,
        "provider": record.provider,
        "model_name": record.model_name,
        "provider_status": record.provider_status,
        "validation_status": record.validation_status,
        "privacy_decision": record.privacy_decision,
        "created_at": record.created_at,
        "payload": payload,
        "payload_checksum": checksum,
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


def test_postgres_m7_model_audit_is_atomic_idempotent_and_purgeable() -> None:
    audit = _model_audit()

    def respond(statement: str, _: tuple[Any, ...]):
        if "FROM m7_model_invocation_audits" in statement:
            return _model_audit_row(audit)
        if "DELETE FROM m7_model_invocation_audits" in statement:
            return [{"invocation_id": audit.invocation_id}]
        return None

    connection = FakeConnection(respond)
    repository = PostgresM7Repository(FakePool(connection))

    repository.save_execution_audit(audit)
    repository.save_execution_audit(audit)
    assert repository.get_execution_audit(audit.invocation_id) == audit
    assert repository.purge_execution_audits_before(
        NOW + timedelta(days=181)
    ) == 1

    insert_parameters = next(
        parameters
        for statement, parameters in connection.executions
        if "INSERT INTO m7_model_invocation_audits" in statement
    )
    payload = next(
        parameter
        for parameter in insert_parameters
        if isinstance(parameter, Jsonb)
    ).obj
    assert payload == audit.to_dict()
    assert not {
        "student_answer",
        "messages",
        "prompt",
        "provider_response",
        "structured_output",
        "content",
    } & set(payload)
    assert connection.transaction_entries == 3


def test_postgres_m7_model_audit_revalidates_checksum() -> None:
    audit = _model_audit()
    corrupt = {**_model_audit_row(audit), "payload_checksum": "f" * 64}
    repository = PostgresM7Repository(
        FakePool(
            FakeConnection(
                lambda statement, _parameters: (
                    corrupt
                    if "FROM m7_model_invocation_audits" in statement
                    else None
                )
            )
        )
    )

    with pytest.raises(PostgresOperationError, match="checksum"):
        repository.get_execution_audit(audit.invocation_id)


def test_postgres_m7_normalizes_checksum_valid_legacy_feedback_quotes() -> None:
    feedback = _feedback()
    payload = feedback.to_dict()
    payload["evidence_citations"][0]["quote"] = "legacy answer text"
    row = {
        **_feedback_row(feedback),
        "payload": payload,
        "payload_checksum": _feedback_payload_checksum(payload),
    }
    repository = PostgresM7Repository(
        FakePool(
            FakeConnection(
                lambda statement, _parameters: (
                    row if "FROM m7_student_feedback" in statement else None
                )
            )
        )
    )

    loaded = repository.get_feedback(feedback.feedback_id)
    assert loaded == feedback
    assert (
        loaded.evidence_citations[0].quote
        == STUDENT_CITATION_QUOTE_PLACEHOLDER
    )
    assert "legacy answer text" not in loaded.to_json()


def test_postgres_m7_normalizes_checksum_valid_pre_quote_feedback() -> None:
    feedback = _feedback()
    payload = feedback.to_dict()
    for citation in payload["evidence_citations"]:
        citation.pop("quote")
    row = {
        **_feedback_row(feedback),
        "payload": payload,
        "payload_checksum": _feedback_payload_checksum(payload),
    }
    repository = PostgresM7Repository(
        FakePool(
            FakeConnection(
                lambda statement, _parameters: (
                    row if "FROM m7_student_feedback" in statement else None
                )
            )
        )
    )

    loaded = repository.get_feedback(feedback.feedback_id)
    assert loaded == feedback
    assert (
        loaded.evidence_citations[0].quote
        == STUDENT_CITATION_QUOTE_PLACEHOLDER
    )


def test_postgres_m7_rejects_checksum_valid_mixed_feedback_quote_shape() -> None:
    feedback = _feedback()
    payload = feedback.to_dict()
    payload["evidence_citations"].append(
        {
            "evidence_id": "evidence_2",
            "source_id": "source_2",
            "locator": "p.2",
        }
    )
    row = {
        **_feedback_row(feedback),
        "payload": payload,
        "payload_checksum": _feedback_payload_checksum(payload),
    }
    repository = PostgresM7Repository(
        FakePool(
            FakeConnection(
                lambda statement, _parameters: (
                    row if "FROM m7_student_feedback" in statement else None
                )
            )
        )
    )

    with pytest.raises(PostgresOperationError, match="integrity check failed"):
        repository.get_feedback(feedback.feedback_id)
