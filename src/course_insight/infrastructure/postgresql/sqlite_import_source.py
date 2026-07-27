"""Read-only SQLite source validation for the PostgreSQL import."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from course_insight.contracts.base import ContractModel
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.tutoring import TutoringControlResult
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.migrations import SCHEMA_VERSION
from course_insight.modules.m0_platform.outbox import (
    OutboxRecord,
    parse_utc_text,
    validate_serialized_record,
)
from course_insight.modules.m0_platform.workflow import AssessmentRun
from course_insight.modules.m6_tutoring_fsm.identity import EvidenceIdentity
from course_insight.modules.m6_tutoring_fsm.repository import (
    TutoringDecisionRecord,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
    PolicyEvaluationRecord,
    PolicyExecutionRef,
    PolicyObservation,
    PolicyRewardRecord,
)

from course_insight.infrastructure.postgresql.sqlite_import import (
    PreparedImportRow,
    _CONTRACT_TABLES,
    _IDENTITY_COLUMNS,
    _SOURCE_SELECTS,
    _TABLE_ORDER,
    _digest,
)
from course_insight.infrastructure.postgresql.sqlite_import_destination import (
    _COLUMNS as _TARGET_COLUMNS,
)

_POLICY_RECORD_TYPES = {
    "m6_policy_artifacts": PolicyArtifactManifest,
    "m6_policy_executions": PolicyExecutionRef,
    "m6_policy_observations": PolicyObservation,
    "m6_policy_rewards": PolicyRewardRecord,
    "m6_policy_evaluations": PolicyEvaluationRecord,
}


def read_and_validate_source(
    source: Path,
) -> tuple[dict[str, tuple[PreparedImportRow, ...]], int, str]:
    """Read one stable query-only snapshot and validate every supported row."""

    uri = f"{source.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        _validate_source_schema(connection)
        out: dict[str, tuple[PreparedImportRow, ...]] = {}
        partial_count = 0
        for table in _TABLE_ORDER:
            prepared_rows: list[PreparedImportRow] = []
            for row in connection.execute(_SOURCE_SELECTS[table]).fetchall():
                prepared = _prepare_row(table, row)
                prepared_rows.append(prepared)
                if not prepared.contract_validated:
                    partial_count += 1
            out[table] = tuple(prepared_rows)
        snapshot_checksum = _digest(
            (
                tuple(
                    (
                        table,
                        tuple(row.fingerprint for row in out[table]),
                    )
                    for table in _TABLE_ORDER
                ),
                partial_count,
            )
        )
        connection.execute("ROLLBACK")
        return out, partial_count, snapshot_checksum
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def _validate_source_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    versions = tuple(int(row[0]) for row in rows)
    if versions != tuple(range(1, SCHEMA_VERSION + 1)):
        raise ValueError("source schema migration ledger is incompatible")
    missing = [
        table
        for table in _TABLE_ORDER
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is None
    ]
    if missing:
        raise ValueError("source schema is missing required tables")
    for table in _TABLE_ORDER:
        actual_columns = tuple(
            str(row["name"])
            for row in connection.execute(
                f"PRAGMA table_info('{table}')"
            ).fetchall()
        )
        expected_columns = (
            _TARGET_COLUMNS[table]
            if table in _POLICY_RECORD_TYPES
            else tuple(
                column
                for column in _TARGET_COLUMNS[table]
                if column not in {"payload_checksum", "schema_version"}
            )
        )
        if actual_columns != expected_columns:
            raise ValueError("source table shape is incompatible")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source foreign-key integrity check failed")


def _prepare_row(table: str, row: sqlite3.Row) -> PreparedImportRow:
    if table == "m0_learning_events":
        return _prepare_learning_event(row)
    if table == "m0_event_outbox":
        return _prepare_outbox(row)
    if table == "m0_assessment_runs":
        return _prepare_assessment_run(row)
    if table == "m6_tutoring_decisions":
        return _prepare_tutoring_decision(row)
    if table in _POLICY_RECORD_TYPES:
        return _prepare_policy_record(table, row)
    contract_type = _CONTRACT_TABLES[table]
    payload = _canonical_json_object(row["payload"])
    contract = contract_type.model_validate(payload)
    _require_current_schema(contract)
    columns = tuple(
        key for key in row.keys() if key != "payload"
    ) + ("payload", "payload_checksum", "schema_version")
    values_by_column = {
        **{key: row[key] for key in row.keys() if key != "payload"},
        "payload": dumps_json(contract.to_dict()),
        "payload_checksum": contract.content_checksum(),
        "schema_version": contract.schema_version,
    }
    _validate_contract_identity(table, row, contract)
    return _build_prepared(
        table,
        columns,
        values_by_column,
        version=_contract_version(table, row, contract),
        checksum=contract.content_checksum(),
        contract_validated=True,
    )


def _prepare_learning_event(row: sqlite3.Row) -> PreparedImportRow:
    event_id = _required_text(row, "event_id")
    idempotency_key = _required_text(row, "idempotency_key")
    event_type = _required_text(row, "event_type")
    occurred_at = _aware_datetime(row["occurred_at"])
    payload = _canonical_json_object(row["payload"])
    if idempotency_key != event_id:
        raise ValueError("event idempotency identity is invalid")
    record = row["_outbox_record"]
    contract_validated = record is not None
    if record is not None:
        validate_serialized_record(event_id, str(record))
        event = LearningEvent.model_validate_json(str(record))
        if (
            event.event_id != event_id
            or event.idempotency_key() != idempotency_key
            or event.event_type != event_type
            or event.occurred_at != occurred_at
            or event.payload != payload
        ):
            raise ValueError("event columns do not match its outbox contract")
        checksum = event.content_checksum()
    else:
        checksum = _digest(
            (event_id, idempotency_key, event_type, occurred_at, payload)
        )
    return _build_prepared(
        "m0_learning_events",
        (
            "event_id",
            "idempotency_key",
            "event_type",
            "occurred_at",
            "payload",
        ),
        {
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "payload": dumps_json(payload),
        },
        version=(),
        checksum=checksum,
        contract_validated=contract_validated,
    )


def _prepare_outbox(row: sqlite3.Row) -> PreparedImportRow:
    event_id = _required_text(row, "event_id")
    record = str(row["record"])
    validate_serialized_record(event_id, record)
    event = LearningEvent.model_validate_json(record)
    outbox = OutboxRecord(
        event_id=event_id,
        serialized_record=record,
        status=str(row["status"]),
        attempt_count=_nonnegative_int(row, "attempt_count"),
        version=_positive_int(row, "version"),
        available_at=parse_utc_text(str(row["available_at"]), field="available_at"),
        locked_by=None if row["locked_by"] is None else str(row["locked_by"]),
        locked_at=_optional_utc(row["locked_at"], "locked_at"),
        lease_until=_optional_utc(row["lease_until"], "lease_until"),
        last_error_code=(
            None
            if row["last_error_code"] is None
            else str(row["last_error_code"])
        ),
        created_at=parse_utc_text(str(row["created_at"]), field="created_at"),
        updated_at=parse_utc_text(str(row["updated_at"]), field="updated_at"),
    )
    values = {
        "event_id": outbox.event_id,
        "record": outbox.serialized_record,
        "status": outbox.status,
        "attempt_count": outbox.attempt_count,
        "version": outbox.version,
        "available_at": outbox.available_at,
        "locked_by": outbox.locked_by,
        "locked_at": outbox.locked_at,
        "lease_until": outbox.lease_until,
        "last_error_code": outbox.last_error_code,
        "created_at": outbox.created_at,
        "updated_at": outbox.updated_at,
    }
    return _build_prepared(
        "m0_event_outbox",
        tuple(values),
        values,
        version=(outbox.version, outbox.attempt_count, outbox.status),
        checksum=event.content_checksum(),
        contract_validated=True,
    )


def _prepare_assessment_run(row: sqlite3.Row) -> PreparedImportRow:
    values = {key: row[key] for key in row.keys()}
    for field in ("lease_until", "created_at", "updated_at"):
        values[field] = (
            None
            if row[field] is None
            else _aware_datetime(row[field])
        )
    run = AssessmentRun(
        operation_id=_required_text(row, "operation_id"),
        operation=str(row["operation"]),
        request_checksum=_required_text(row, "request_checksum"),
        course_id=_required_text(row, "course_id"),
        class_id=_required_text(row, "class_id"),
        learner_id=_required_text(row, "learner_id"),
        session_id=_required_text(row, "session_id"),
        task_id=_required_text(row, "task_id"),
        paper_id=_required_text(row, "paper_id"),
        attempt_id=_optional_text(row, "attempt_id"),
        feedback_id=_optional_text(row, "feedback_id"),
        report_id=_optional_text(row, "report_id"),
        checkpoint=_required_text(row, "checkpoint"),
        status=str(row["status"]),
        version=_positive_int(row, "version"),
        locked_by=_optional_text(row, "locked_by"),
        lease_until=values["lease_until"],
        error_code=_optional_text(row, "error_code"),
        created_at=values["created_at"],
        updated_at=values["updated_at"],
        scoring_result_checksum=_optional_text(
            row,
            "scoring_result_checksum",
        ),
        target_audit_id=_optional_text(row, "target_audit_id"),
        target_audit_version=_optional_positive_int(
            row,
            "target_audit_version",
        ),
        state_version=_optional_positive_int(row, "state_version"),
        knowledge_bundle_id=_optional_text(row, "knowledge_bundle_id"),
        knowledge_bundle_version=_optional_text(
            row,
            "knowledge_bundle_version",
        ),
        knowledge_bundle_checksum=_optional_text(
            row,
            "knowledge_bundle_checksum",
        ),
        course_package_id=_optional_text(row, "course_package_id"),
        evidence_index_id=_optional_text(row, "evidence_index_id"),
        evidence_index_version=_optional_text(
            row,
            "evidence_index_version",
        ),
        evidence_index_checksum=_optional_text(
            row,
            "evidence_index_checksum",
        ),
        state_policy_checksum=_optional_text(
            row,
            "state_policy_checksum",
        ),
        teacher_policy_checksum=_optional_text(
            row,
            "teacher_policy_checksum",
        ),
        previous_state_frozen=_optional_bool(
            row,
            "previous_state_frozen",
        ),
        previous_learner_snapshot_id=_optional_text(
            row,
            "previous_learner_snapshot_id",
        ),
        previous_learner_state_version=_optional_positive_int(
            row,
            "previous_learner_state_version",
        ),
        previous_class_snapshot_id=_optional_text(
            row,
            "previous_class_snapshot_id",
        ),
        previous_class_state_version=_optional_positive_int(
            row,
            "previous_class_state_version",
        ),
    )
    values["previous_state_frozen"] = run.previous_state_frozen
    return _build_prepared(
        "m0_assessment_runs",
        tuple(values),
        values,
        version=(run.version, run.checkpoint, run.status),
        checksum=_digest(tuple(values.values())),
        contract_validated=True,
    )


def _prepare_tutoring_decision(row: sqlite3.Row) -> PreparedImportRow:
    evidence_payload = _canonical_json_object(row["evidence_identity"])
    result_payload = _canonical_json_object(row["result_payload"])
    evidence = EvidenceIdentity.from_dict(evidence_payload)
    result = TutoringControlResult.model_validate(result_payload)
    _require_current_schema(result)
    record = TutoringDecisionRecord(
        decision_id=_required_text(row, "decision_id"),
        session_id=_required_text(row, "session_id"),
        turn_count=_nonnegative_int(row, "turn_count"),
        previous_turn_count=_optional_nonnegative_int(
            row,
            "previous_turn_count",
        ),
        request_fingerprint=_required_sha256(row, "request_fingerprint"),
        input_fingerprint=_required_sha256(row, "input_fingerprint"),
        evidence_identity=evidence,
        result=result,
    )
    if record.evidence_fingerprint != _required_sha256(
        row,
        "evidence_fingerprint",
    ):
        raise ValueError("tutoring evidence fingerprint is invalid")
    values = {
        "decision_id": record.decision_id,
        "session_id": record.session_id,
        "turn_count": record.turn_count,
        "previous_turn_count": record.previous_turn_count,
        "request_fingerprint": record.request_fingerprint,
        "input_fingerprint": record.input_fingerprint,
        "evidence_fingerprint": record.evidence_fingerprint,
        "evidence_identity": dumps_json(evidence.to_dict()),
        "result_payload": dumps_json(result.to_dict()),
        "payload_checksum": result.content_checksum(),
        "schema_version": result.schema_version,
    }
    return _build_prepared(
        "m6_tutoring_decisions",
        tuple(values),
        values,
        version=(record.turn_count, result.schema_version),
        checksum=result.content_checksum(),
        contract_validated=True,
    )


def _prepare_policy_record(
    table: str,
    row: sqlite3.Row,
) -> PreparedImportRow:
    source_payload = row["payload"]
    payload = _canonical_json_object(source_payload)
    record_type = _POLICY_RECORD_TYPES[table]
    values = dict(payload)
    if record_type is PolicyArtifactManifest:
        allowed_scopes = values.get("allowed_scopes")
        if type(allowed_scopes) is not list:
            raise ValueError("policy allowed scopes are invalid")
        values["allowed_scopes"] = tuple(allowed_scopes)
    elif record_type is PolicyObservation:
        candidate_ids = values.get("candidate_ids")
        if type(candidate_ids) is not list:
            raise ValueError("policy candidate IDs are invalid")
        values["candidate_ids"] = tuple(candidate_ids)
    record = record_type(**values)
    canonical_payload = dumps_json(record.canonical_payload())
    if source_payload != canonical_payload:
        raise ValueError("policy payload is not exact canonical JSON")
    checksum = _required_sha256(row, "payload_checksum")
    if record.identity != checksum:
        raise ValueError("policy payload checksum is invalid")
    _validate_policy_identity(table, row, record)
    columns = tuple(row.keys())
    row_values = {
        column: (
            canonical_payload
            if column == "payload"
            else row[column]
        )
        for column in columns
    }
    return _build_prepared(
        table,
        columns,
        row_values,
        version=_policy_version(record),
        checksum=checksum,
        contract_validated=True,
    )


def _validate_policy_identity(table: str, row: sqlite3.Row, record: Any) -> None:
    if table == "m6_policy_artifacts":
        checks = (
            ("policy_id", record.policy_id),
            ("artifact_sha256", record.artifact_sha256),
        )
    elif table == "m6_policy_executions":
        checks = (
            ("request_fingerprint", record.request_fingerprint),
            ("policy_execution_fingerprint", record.identity),
        )
    elif table == "m6_policy_observations":
        checks = (
            ("request_fingerprint", record.request_fingerprint),
            ("policy_execution_fingerprint", record.policy_execution_fingerprint),
        )
    elif table == "m6_policy_rewards":
        checks = (
            ("reward_identity", record.identity),
            ("policy_execution_fingerprint", record.policy_execution_fingerprint),
            ("reward_version", record.reward_version),
        )
    else:
        checks = (
            ("evaluation_identity", record.identity),
            ("policy_id", record.policy_id),
            ("dataset_identity", record.dataset_identity),
        )
    if any(row[column] != expected for column, expected in checks):
        raise ValueError("policy identity does not match its row")


def _policy_version(record: Any) -> tuple[object, ...]:
    if isinstance(record, PolicyArtifactManifest):
        return (record.adapter_version, record.status)
    if isinstance(record, PolicyExecutionRef):
        return (record.adapter_version, record.mode)
    if isinstance(record, PolicyObservation):
        return (record.feature_schema_version,)
    if isinstance(record, PolicyRewardRecord):
        return (record.reward_version, record.status)
    return (record.status, record.approved)


def _validate_contract_identity(
    table: str,
    row: sqlite3.Row,
    contract: ContractModel,
) -> None:
    checks: tuple[tuple[str, object], ...]
    if table == "m4_task_plans":
        try:
            idempotency_key = _required_sha256(row, "idempotency_key")
        except ValueError:
            raise ValueError("M4 task identity is invalid") from None
        if contract.task_id != f"task_{idempotency_key}":
            raise ValueError("M4 task identity is invalid")
        checks = (
            ("task_id", contract.task_id),
            ("idempotency_key", idempotency_key),
        )
    elif table == "m5_learner_states":
        checks = tuple(
            (field, getattr(contract, field))
            for field in (
                "snapshot_id",
                "course_id",
                "class_id",
                "learner_id",
                "state_version",
            )
        )
    elif table == "m5_class_states":
        _positive_int(row, "state_version")
        checks = tuple(
            (field, getattr(contract, field))
            for field in (
                "snapshot_id",
                "course_id",
                "class_id",
                "aggregation_policy_version",
            )
        )
    elif table == "m5_state_updates":
        learner = contract.learner_state_snapshot
        checks = (
            ("attempt_id", contract.diagnosis_result.attempt_id),
            ("course_id", learner.course_id),
            ("class_id", learner.class_id),
            ("learner_id", learner.learner_id),
            ("state_version", learner.state_version),
        )
    elif table == "m6_session_states":
        checks = (
            ("session_id", contract.session_id),
            ("turn_count", contract.turn_count),
        )
    elif table == "m7_student_feedback":
        checks = (
            ("feedback_id", contract.feedback_id),
            ("task_id", contract.task_id),
            ("learner_id", contract.learner_id),
        )
    elif table == "m8_assessment_papers":
        checks = (
            ("paper_id", contract.paper_id),
            ("task_id", contract.task_id),
            ("course_id", _required_text(row, "course_id")),
            ("class_id", _required_text(row, "class_id")),
            ("learner_id", contract.learner_id),
        )
    elif table == "m8_score_audits":
        checks = tuple(
            (field, getattr(contract, field))
            for field in ("audit_id", "audit_version", "item_instance_id")
        )
    elif table == "m8_scoring_results":
        checks = (
            ("attempt_id", contract.attempt_id),
            ("result_key", _scoring_result_key(contract)),
            ("paper_id", contract.paper_id),
            ("learner_id", contract.learner_id),
            ("finalized_at", contract.finalized_at.isoformat()),
        )
    elif table == "m9_teacher_reviews":
        checks = tuple(
            (field, getattr(contract, field))
            for field in (
                "decision_id",
                "audit_id",
                "expected_audit_version",
            )
        )
    elif table == "m9_teacher_analytics":
        checks = (
            ("report_id", contract.report_id),
            ("course_id", _required_text(row, "course_id")),
            ("class_id", contract.class_report.class_id),
            ("generated_at", contract.generated_at.isoformat()),
            (
                "learner_ids",
                dumps_json(
                    sorted(
                        report.learner_id
                        for report in contract.individual_reports
                    )
                ),
            ),
        )
    else:
        raise ValueError("unsupported contract table")
    for column, expected in checks:
        actual = row[column]
        if isinstance(expected, int):
            actual = int(actual)
        elif not isinstance(expected, str):
            actual = str(actual)
        if actual != expected:
            raise ValueError("contract identity does not match its row")


def _scoring_result_key(contract: Any) -> str:
    latest_versions: dict[str, int] = {}
    for record in contract.score_audit_records:
        latest_versions = {
            **latest_versions,
            record.audit_id: max(
                record.audit_version,
                latest_versions.get(record.audit_id, 0),
            ),
        }
    return dumps_json(
        [
            {"audit_id": audit_id, "audit_version": audit_version}
            for audit_id, audit_version in sorted(latest_versions.items())
        ]
    )


def _contract_version(
    table: str,
    row: sqlite3.Row,
    contract: ContractModel,
) -> tuple[object, ...]:
    columns = {
        "m5_learner_states": ("state_version",),
        "m5_class_states": ("state_version",),
        "m5_state_updates": ("state_version",),
        "m6_session_states": ("turn_count",),
        "m8_score_audits": ("audit_version",),
        "m9_teacher_reviews": ("expected_audit_version",),
    }.get(table, ())
    return tuple(row[column] for column in columns) + (
        contract.schema_version,
    )


def _build_prepared(
    table: str,
    columns: tuple[str, ...],
    values_by_column: dict[str, Any],
    *,
    version: tuple[object, ...],
    checksum: str,
    contract_validated: bool,
) -> PreparedImportRow:
    if table not in _TABLE_ORDER or len(set(columns)) != len(columns):
        raise ValueError("import table definition is invalid")
    values = tuple(values_by_column[column] for column in columns)
    mapping = dict(zip(columns, values, strict=True))
    identity = tuple(
        mapping[column] for column in _IDENTITY_COLUMNS[table]
    )
    if any(value is None or value == "" for value in identity):
        raise ValueError("row identity is invalid")
    return PreparedImportRow(
        table=table,
        columns=columns,
        values=values,
        identity=identity,
        version=version,
        checksum=checksum,
        fingerprint=_digest(
            (table, tuple(zip(columns, values, strict=True)))
        ),
        contract_validated=contract_validated,
    )


def _canonical_json_object(value: object) -> dict[str, Any]:
    if type(value) is not str:
        raise ValueError("persisted JSON must be text")
    try:
        decoded = json.loads(value, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("persisted JSON is invalid") from None
    if type(decoded) is not dict or dumps_json(decoded) != value:
        raise ValueError("persisted JSON must be a canonical object")
    return decoded


def _reject_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON is invalid")


def _require_current_schema(contract: ContractModel) -> None:
    expected = str(type(contract).model_fields["schema_version"].default)
    if contract.schema_version != expected:
        raise ValueError("contract schema version is unsupported")


def _required_text(row: Any, field: str) -> str:
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError(f"{field} is invalid")
    value.encode("utf-8")
    return value


def _optional_text(row: sqlite3.Row, field: str) -> str | None:
    return None if row[field] is None else _required_text(row, field)


def _positive_int(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} is invalid")
    return value


def _nonnegative_int(row: sqlite3.Row, field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} is invalid")
    return value


def _optional_positive_int(row: sqlite3.Row, field: str) -> int | None:
    return None if row[field] is None else _positive_int(row, field)


def _optional_bool(row: sqlite3.Row, field: str) -> bool | None:
    value = row[field]
    if value is None:
        return None
    if type(value) is not int or value not in {0, 1}:
        raise ValueError(f"{field} is invalid")
    return bool(value)


def _optional_nonnegative_int(row: sqlite3.Row, field: str) -> int | None:
    return None if row[field] is None else _nonnegative_int(row, field)


def _required_sha256(row: Any, field: str) -> str:
    value = _required_text(row, field)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _aware_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _optional_utc(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return parse_utc_text(str(value), field=field)


__all__ = ["read_and_validate_source"]
