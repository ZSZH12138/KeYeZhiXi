"""PostgreSQL persistence for replay-safe M6 tutoring decisions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from course_insight.contracts.errors import DomainError
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    derive_identifier,
)
from course_insight.modules.m6_tutoring_fsm.repository import (
    TutoringDecisionRecord,
    assert_same_session_snapshot,
    isolated_session_snapshot,
    validate_decision_commit,
    validate_session_append,
)


_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M6 repository integrity check failed"
_SNAPSHOT_COLUMNS = """
session_id,
turn_count,
payload,
payload_checksum,
schema_version
"""
_DECISION_COLUMNS = """
decision_id,
session_id,
turn_count,
previous_turn_count,
request_fingerprint,
input_fingerprint,
evidence_fingerprint,
evidence_identity,
result_payload,
payload_checksum,
schema_version
"""
_DECISION_LOOKUP_COLUMNS = frozenset(
    {"request_fingerprint", "input_fingerprint"}
)


class PostgresM6Repository:
    """Persist append-only M6 histories and atomic tutoring decisions."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def initialize(self) -> None:
        """Apply every verified PostgreSQL migration."""

        from course_insight.infrastructure.postgresql.migration_runner import (
            run_migrations,
        )

        run_migrations(self._pool)

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        """Insert one immutable snapshot without overwriting history."""

        candidate = isolated_session_snapshot(snapshot)
        _require_current_contract_schemas(candidate)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _lock_session(connection, candidate.session_id)
                    existing_row = _snapshot_row(
                        connection,
                        candidate.session_id,
                        candidate.turn_count,
                    )
                    if existing_row is not None:
                        assert_same_session_snapshot(
                            _snapshot_from_row(existing_row),
                            candidate,
                        )
                        return
                    latest_row = _latest_snapshot_row(
                        connection,
                        candidate.session_id,
                    )
                    validate_session_append(
                        (
                            None
                            if latest_row is None
                            else _snapshot_from_row(latest_row)
                        ),
                        candidate,
                    )
                    _insert_snapshot(connection, candidate)
                    stored_row = _snapshot_row(
                        connection,
                        candidate.session_id,
                        candidate.turn_count,
                    )
                    if stored_row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    assert_same_session_snapshot(
                        _snapshot_from_row(stored_row),
                        candidate,
                    )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        """Load one exact session turn as an isolated contract object."""

        try:
            with self._pool.connection() as connection:
                row = _snapshot_row(connection, session_id, turn_count)
                return None if row is None else _snapshot_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        """Load the greatest persisted turn for one session."""

        try:
            with self._pool.connection() as connection:
                row = _latest_snapshot_row(connection, session_id)
                return None if row is None else _snapshot_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_decision_by_request(
        self,
        request_fingerprint: str,
    ) -> TutoringDecisionRecord | None:
        """Load one exact public-request replay, if present."""

        try:
            with self._pool.connection() as connection:
                row = _decision_row(
                    connection,
                    "request_fingerprint",
                    request_fingerprint,
                )
                return None if row is None else _decision_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_decision(
        self,
        session_id: str,
    ) -> TutoringDecisionRecord | None:
        """Load the greatest evidence-bearing decision for one session."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_DECISION_COLUMNS}
                    FROM m6_tutoring_decisions
                    WHERE session_id = %s
                    ORDER BY turn_count DESC
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                return None if row is None else _decision_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def commit_decision(
        self,
        record: TutoringDecisionRecord,
        expected_previous_snapshot: SessionStateSnapshot,
    ) -> TutoringDecisionRecord:
        """Atomically seed history, advance one turn, and store a decision."""

        if not isinstance(record, TutoringDecisionRecord):
            raise TypeError("record must be a TutoringDecisionRecord")
        candidate = record.isolated_copy()
        expected = isolated_session_snapshot(expected_previous_snapshot)
        _require_current_contract_schemas(candidate.result)
        _require_current_contract_schemas(expected)
        validate_decision_commit(candidate, expected)
        result_snapshot = candidate.result.session_state_snapshot

        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _lock_session(connection, candidate.session_id)
                    request_row = _decision_row(
                        connection,
                        "request_fingerprint",
                        candidate.request_fingerprint,
                    )
                    if request_row is not None:
                        authoritative = _decision_from_row(request_row)
                        _assert_replay_compatible(
                            authoritative,
                            candidate,
                            allow_request_mismatch=False,
                        )
                        return authoritative
                    input_row = _decision_row(
                        connection,
                        "input_fingerprint",
                        candidate.input_fingerprint,
                    )
                    if input_row is not None:
                        authoritative = _decision_from_row(input_row)
                        _assert_replay_compatible(
                            authoritative,
                            candidate,
                            allow_request_mismatch=True,
                        )
                        return authoritative

                    latest_row = _latest_snapshot_row(
                        connection,
                        candidate.session_id,
                    )
                    if latest_row is None:
                        _insert_snapshot(connection, expected)
                    else:
                        assert_same_session_snapshot(
                            _snapshot_from_row(latest_row),
                            expected,
                        )
                    _insert_snapshot(connection, result_snapshot)
                    _insert_decision(connection, candidate)

                    stored_snapshot_row = _snapshot_row(
                        connection,
                        candidate.session_id,
                        candidate.turn_count,
                    )
                    if stored_snapshot_row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    assert_same_session_snapshot(
                        _snapshot_from_row(stored_snapshot_row),
                        result_snapshot,
                    )
                    stored_row = _decision_row(
                        connection,
                        "request_fingerprint",
                        candidate.request_fingerprint,
                    )
                    if stored_row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    stored = _decision_from_row(stored_row)
                    _assert_replay_compatible(
                        stored,
                        candidate,
                        allow_request_mismatch=False,
                    )
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _lock_session(connection: Any, session_id: str) -> None:
    digest = hashlib.sha256(
        f"course-insight:m6:{session_id}".encode("utf-8")
    ).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    connection.execute(
        "SELECT pg_advisory_xact_lock(%s)",
        (lock_key,),
    )


def _snapshot_row(
    connection: Any,
    session_id: str,
    turn_count: int,
) -> Any:
    return connection.execute(
        f"""
        SELECT {_SNAPSHOT_COLUMNS}
        FROM m6_session_states
        WHERE session_id = %s AND turn_count = %s
        """,
        (session_id, turn_count),
    ).fetchone()


def _latest_snapshot_row(connection: Any, session_id: str) -> Any:
    return connection.execute(
        f"""
        SELECT {_SNAPSHOT_COLUMNS}
        FROM m6_session_states
        WHERE session_id = %s
        ORDER BY turn_count DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def _decision_row(connection: Any, column: str, value: str) -> Any:
    if column not in _DECISION_LOOKUP_COLUMNS:
        raise ValueError("unsupported M6 decision lookup column")
    return connection.execute(
        f"""
        SELECT {_DECISION_COLUMNS}
        FROM m6_tutoring_decisions
        WHERE {column} = %s
        """,
        (value,),
    ).fetchone()


def _insert_snapshot(
    connection: Any,
    snapshot: SessionStateSnapshot,
) -> None:
    connection.execute(
        """
        INSERT INTO m6_session_states(
            session_id,
            turn_count,
            payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            snapshot.session_id,
            snapshot.turn_count,
            Jsonb(snapshot.to_dict()),
            snapshot.content_checksum(),
            snapshot.schema_version,
        ),
    )


def _insert_decision(
    connection: Any,
    record: TutoringDecisionRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO m6_tutoring_decisions(
            decision_id,
            session_id,
            turn_count,
            previous_turn_count,
            request_fingerprint,
            input_fingerprint,
            evidence_fingerprint,
            evidence_identity,
            result_payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            record.decision_id,
            record.session_id,
            record.turn_count,
            record.previous_turn_count,
            record.request_fingerprint,
            record.input_fingerprint,
            record.evidence_fingerprint,
            Jsonb(record.evidence_identity.to_dict()),
            Jsonb(record.result.to_dict()),
            record.result.content_checksum(),
            record.result.schema_version,
        ),
    )


def _snapshot_from_row(row: Any) -> SessionStateSnapshot:
    try:
        session_id = _required_string(row, "session_id")
        turn_count = _required_integer(row, "turn_count")
        payload = _required_json_object(row, "payload")
        snapshot = SessionStateSnapshot.model_validate(payload)
        _require_current_contract_schemas(snapshot)
        _verify_payload_metadata(snapshot, row)
        if (
            snapshot.session_id != session_id
            or snapshot.turn_count != turn_count
        ):
            raise ValueError("snapshot columns do not match payload")
        return snapshot
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _decision_from_row(row: Any) -> TutoringDecisionRecord:
    try:
        evidence_payload = _required_json_object(row, "evidence_identity")
        result_payload = _required_json_object(row, "result_payload")
        evidence_identity = EvidenceIdentity.from_dict(evidence_payload)
        result = TutoringControlResult.model_validate(result_payload)
        _require_current_contract_schemas(result)
        _verify_payload_metadata(result, row)
        previous_value = row["previous_turn_count"]
        if previous_value is not None and type(previous_value) is not int:
            raise ValueError("previous turn must be an integer or null")
        record = TutoringDecisionRecord(
            decision_id=_required_string(row, "decision_id"),
            session_id=_required_string(row, "session_id"),
            turn_count=_required_integer(row, "turn_count"),
            previous_turn_count=previous_value,
            request_fingerprint=_required_sha256(
                row,
                "request_fingerprint",
            ),
            input_fingerprint=_required_sha256(row, "input_fingerprint"),
            evidence_identity=evidence_identity,
            result=result,
        )
        if (
            record.evidence_fingerprint
            != _required_sha256(row, "evidence_fingerprint")
            or result.teaching_action.action_id
            != derive_identifier("action", record.input_fingerprint)
            or result.evidence_query.query_id
            != derive_identifier("query", record.input_fingerprint)
            or result.feedback_generation_task.feedback_task_id
            != derive_identifier("feedback_task", record.input_fingerprint)
        ):
            raise ValueError("decision identity does not match payload")
        return record
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _verify_payload_metadata(model: BaseModel, row: Any) -> None:
    stored_checksum = _required_sha256(row, "payload_checksum")
    stored_schema_version = _required_string(row, "schema_version")
    actual_schema_version = getattr(model, "schema_version", None)
    if (
        actual_schema_version != stored_schema_version
        or model.content_checksum() != stored_checksum
    ):
        raise ValueError("payload metadata is inconsistent")


def _require_current_contract_schemas(value: Any) -> None:
    if isinstance(value, BaseModel):
        field = type(value).model_fields.get("schema_version")
        if field is not None and getattr(value, "schema_version") != field.default:
            raise ValueError("persisted contract schema version is unsupported")
        for field_name in type(value).model_fields:
            _require_current_contract_schemas(getattr(value, field_name))
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            _require_current_contract_schemas(nested)
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for nested in value:
            _require_current_contract_schemas(nested)


def _required_json_object(row: Any, field: str) -> dict[str, Any]:
    value = row[field]
    if type(value) is not dict:
        raise ValueError(f"{field} must be a JSON object")
    return value


def _required_string(row: Any, field: str) -> str:
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _required_integer(row: Any, field: str) -> int:
    value = row[field]
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _required_sha256(row: Any, field: str) -> str:
    value = _required_string(row, field)
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _assert_replay_compatible(
    stored: TutoringDecisionRecord,
    candidate: TutoringDecisionRecord,
    *,
    allow_request_mismatch: bool,
) -> None:
    if (
        stored.decision_id != candidate.decision_id
        or stored.session_id != candidate.session_id
        or stored.turn_count != candidate.turn_count
        or stored.previous_turn_count != candidate.previous_turn_count
        or (
            not allow_request_mismatch
            and stored.request_fingerprint != candidate.request_fingerprint
        )
        or stored.input_fingerprint != candidate.input_fingerprint
        or stored.evidence_identity != candidate.evidence_identity
        or stored.evidence_fingerprint != candidate.evidence_fingerprint
        or stored.result.content_checksum()
        != candidate.result.content_checksum()
    ):
        _raise_reference_mismatch("persisted_decision_conflict")


def _raise_reference_mismatch(reason: str) -> None:
    raise DomainError(
        code="TUTORING_REFERENCE_MISMATCH",
        module="m6",
        message="tutoring history conflicts with its persisted authority",
        details={"reason": reason},
    )


PostgreSQLM6Repository = PostgresM6Repository

__all__ = [
    "PostgresM6Repository",
    "PostgresOperationError",
    "PostgreSQLM6Repository",
]
