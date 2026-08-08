from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.m7_repository import SQLiteM7Repository
from course_insight.modules.m7_local_model.repository import M7ModelAuditRecord
from tests.integration.test_web_workflow_persistence import _feedback


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _audit() -> M7ModelAuditRecord:
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


def test_sqlite_m7_model_audit_is_atomic_idempotent_and_purgeable(
    tmp_path,
) -> None:
    database_path = tmp_path / "m7.sqlite3"
    repository = SQLiteM7Repository(database_path)
    repository.initialize()
    audit = _audit()

    repository.save_execution_audit(audit)
    repository.save_execution_audit(audit)
    assert repository.get_execution_audit(audit.invocation_id) == audit

    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            "SELECT payload FROM m7_model_invocation_audits"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload"]))
        assert payload == audit.to_dict()
        assert not {
            "student_answer",
            "messages",
            "prompt",
            "provider_response",
            "structured_output",
            "content",
        } & set(payload)
        assert connection.execute(
            "SELECT COUNT(*) FROM m7_model_invocation_audits"
        ).fetchone()[0] == 1
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="conflict"):
        repository.save_execution_audit(
            replace(audit, invocation_id="invocation_conflict")
        )

    assert repository.purge_execution_audits_before(
        NOW
    ) == 0
    assert repository.get_execution_audit(audit.invocation_id) == audit
    assert repository.purge_execution_audits_before(
        NOW + timedelta(days=181)
    ) == 1
    assert repository.get_execution_audit(audit.invocation_id) is None


def test_sqlite_m7_model_audit_revalidates_checksum(tmp_path) -> None:
    database_path = tmp_path / "m7.sqlite3"
    repository = SQLiteM7Repository(database_path)
    repository.initialize()
    audit = _audit()
    repository.save_execution_audit(audit)

    connection = connect_sqlite(database_path)
    try:
        connection.execute(
            """
            UPDATE m7_model_invocation_audits
            SET payload_checksum = ?
            WHERE invocation_id = ?
            """,
            ("f" * 64, audit.invocation_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="checksum"):
        repository.get_execution_audit(audit.invocation_id)


def test_m7_audit_rejects_free_text_flags_and_error_details() -> None:
    audit = _audit()

    with pytest.raises(ValueError, match="safety flags"):
        replace(
            audit,
            safety_flags=("pii_email_redacted: learner@example.com",),
        )
    with pytest.raises(ValueError, match="error code"):
        replace(audit, error_code="provider said learner@example.com")


def test_sqlite_m7_strips_legacy_feedback_quotes_on_read(tmp_path) -> None:
    database_path = tmp_path / "m7.sqlite3"
    repository = SQLiteM7Repository(database_path)
    repository.initialize()
    feedback = _feedback()
    payload = feedback.to_dict()
    payload["evidence_citations"][0]["quote"] = "legacy answer text"

    connection = connect_sqlite(database_path)
    try:
        connection.execute(
            """
            INSERT INTO m7_student_feedback(
                feedback_id,
                task_id,
                learner_id,
                payload
            ) VALUES (?, ?, ?, ?)
            """,
            (
                feedback.feedback_id,
                feedback.task_id,
                feedback.learner_id,
                dumps_json(payload),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    loaded = repository.get_feedback(feedback.feedback_id)
    assert loaded == feedback
    assert "legacy answer text" not in loaded.to_json()
