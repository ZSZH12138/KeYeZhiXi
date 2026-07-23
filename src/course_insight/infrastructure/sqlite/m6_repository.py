"""SQLite persistence for replay-safe M6 tutoring decisions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.modules.m6_tutoring_fsm.identity import EvidenceIdentity
from course_insight.modules.m6_tutoring_fsm.repository import (
    TutoringDecisionRecord,
    assert_same_session_snapshot,
    isolated_session_snapshot,
    validate_decision_commit,
    validate_session_append,
)


class SQLiteM6Repository:
    """Persist M6 snapshots and decisions in explicit immediate transactions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        """Create the database and apply every pending migration."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        """Insert one immutable snapshot without overwriting existing history."""

        candidate = isolated_session_snapshot(snapshot)
        payload = dumps_json(candidate.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT session_id, turn_count, payload
                FROM m6_session_states
                WHERE session_id = ? AND turn_count = ?
                """,
                (candidate.session_id, candidate.turn_count),
            ).fetchone()
            if existing is not None:
                assert_same_session_snapshot(_snapshot_from_row(existing), candidate)
                connection.execute("COMMIT")
                return
            latest_row = _latest_snapshot_row(connection, candidate.session_id)
            validate_session_append(
                None if latest_row is None else _snapshot_from_row(latest_row),
                candidate,
            )
            _insert_snapshot(connection, candidate, payload)
            row = connection.execute(
                """
                SELECT session_id, turn_count, payload
                FROM m6_session_states
                WHERE session_id = ? AND turn_count = ?
                """,
                (candidate.session_id, candidate.turn_count),
            ).fetchone()
            if row is None:
                raise RuntimeError("M6 snapshot insert produced no authoritative row")
            assert_same_session_snapshot(_snapshot_from_row(row), candidate)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        """Load one exact session turn as an isolated contract object."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT session_id, turn_count, payload
                FROM m6_session_states
                WHERE session_id = ? AND turn_count = ?
                """,
                (session_id, turn_count),
            ).fetchone()
            return None if row is None else _snapshot_from_row(row)
        finally:
            connection.close()

    def get_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        """Load the greatest persisted turn for one session."""

        connection = connect_sqlite(self._database_path)
        try:
            row = _latest_snapshot_row(connection, session_id)
            return None if row is None else _snapshot_from_row(row)
        finally:
            connection.close()

    def get_decision_by_request(
        self,
        request_fingerprint: str,
    ) -> TutoringDecisionRecord | None:
        """Load one prior exact public request result."""

        connection = connect_sqlite(self._database_path)
        try:
            row = _decision_row(
                connection,
                "request_fingerprint",
                request_fingerprint,
            )
            return None if row is None else _decision_from_row(row)
        finally:
            connection.close()

    def get_latest_decision(
        self,
        session_id: str,
    ) -> TutoringDecisionRecord | None:
        """Load the greatest evidence-bearing decision for one session."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                f"""
                SELECT {_DECISION_COLUMNS}
                FROM m6_tutoring_decisions
                WHERE session_id = ?
                ORDER BY turn_count DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return None if row is None else _decision_from_row(row)
        finally:
            connection.close()

    def commit_decision(
        self,
        record: TutoringDecisionRecord,
        expected_previous_snapshot: SessionStateSnapshot,
    ) -> TutoringDecisionRecord:
        """Atomically seed history, advance one turn, and store its decision."""

        candidate = record.isolated_copy()
        expected = isolated_session_snapshot(expected_previous_snapshot)
        validate_decision_commit(candidate, expected)
        result_snapshot = candidate.result.session_state_snapshot
        expected_payload = dumps_json(expected.to_dict())
        result_payload = dumps_json(result_snapshot.to_dict())
        evidence_payload = dumps_json(candidate.evidence_identity.to_dict())
        decision_payload = dumps_json(candidate.result.to_dict())

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = _decision_row(
                connection,
                "request_fingerprint",
                candidate.request_fingerprint,
            )
            if replay is None:
                replay = _decision_row(
                    connection,
                    "input_fingerprint",
                    candidate.input_fingerprint,
                )
            if replay is not None:
                authoritative = _decision_from_row(replay)
                connection.execute("COMMIT")
                return authoritative

            latest_row = _latest_snapshot_row(connection, candidate.session_id)
            if latest_row is None:
                _insert_snapshot(connection, expected, expected_payload)
            else:
                assert_same_session_snapshot(
                    _snapshot_from_row(latest_row),
                    expected,
                )

            _insert_snapshot(connection, result_snapshot, result_payload)
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
                    result_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.decision_id,
                    candidate.session_id,
                    candidate.turn_count,
                    candidate.previous_turn_count,
                    candidate.request_fingerprint,
                    candidate.input_fingerprint,
                    candidate.evidence_fingerprint,
                    evidence_payload,
                    decision_payload,
                ),
            )
            stored_row = _decision_row(
                connection,
                "request_fingerprint",
                candidate.request_fingerprint,
            )
            if stored_row is None:
                raise RuntimeError("M6 decision insert produced no authoritative row")
            stored = _decision_from_row(stored_row)
            _assert_same_decision(stored, candidate)
            connection.execute("COMMIT")
            return stored
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


_DECISION_COLUMNS = """
decision_id,
session_id,
turn_count,
previous_turn_count,
request_fingerprint,
input_fingerprint,
evidence_fingerprint,
evidence_identity,
result_payload
"""
_DECISION_LOOKUP_COLUMNS = frozenset(
    {"request_fingerprint", "input_fingerprint"}
)


def _latest_snapshot_row(
    connection: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT session_id, turn_count, payload
        FROM m6_session_states
        WHERE session_id = ?
        ORDER BY turn_count DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def _decision_row(
    connection: sqlite3.Connection,
    column: str,
    value: str,
) -> sqlite3.Row | None:
    if column not in _DECISION_LOOKUP_COLUMNS:
        raise ValueError("unsupported M6 decision lookup column")
    return connection.execute(
        f"""
        SELECT {_DECISION_COLUMNS}
        FROM m6_tutoring_decisions
        WHERE {column} = ?
        """,
        (value,),
    ).fetchone()


def _insert_snapshot(
    connection: sqlite3.Connection,
    snapshot: SessionStateSnapshot,
    payload: str,
) -> None:
    connection.execute(
        """
        INSERT INTO m6_session_states(session_id, turn_count, payload)
        VALUES (?, ?, ?)
        """,
        (snapshot.session_id, snapshot.turn_count, payload),
    )


def _snapshot_from_row(row: sqlite3.Row) -> SessionStateSnapshot:
    payload = str(row["payload"])
    _require_canonical_json(payload, "session snapshot")
    try:
        snapshot = SessionStateSnapshot.model_validate_json(payload)
    except Exception as error:
        raise RuntimeError("M6 session snapshot payload is invalid") from error
    if (
        snapshot.session_id != str(row["session_id"])
        or snapshot.turn_count != int(row["turn_count"])
    ):
        raise RuntimeError("M6 session snapshot columns do not match payload")
    return snapshot


def _decision_from_row(row: sqlite3.Row) -> TutoringDecisionRecord:
    evidence_payload = str(row["evidence_identity"])
    result_payload = str(row["result_payload"])
    _require_canonical_json(evidence_payload, "evidence identity")
    _require_canonical_json(result_payload, "tutoring result")
    try:
        evidence_value = json.loads(evidence_payload)
        if type(evidence_value) is not dict:
            raise ValueError("evidence identity must be an object")
        evidence_identity = EvidenceIdentity.from_dict(evidence_value)
        result = TutoringControlResult.model_validate_json(result_payload)
        previous_value = row["previous_turn_count"]
        record = TutoringDecisionRecord(
            decision_id=str(row["decision_id"]),
            session_id=str(row["session_id"]),
            turn_count=int(row["turn_count"]),
            previous_turn_count=(
                None if previous_value is None else int(previous_value)
            ),
            request_fingerprint=str(row["request_fingerprint"]),
            input_fingerprint=str(row["input_fingerprint"]),
            evidence_identity=evidence_identity,
            result=result,
        )
    except Exception as error:
        raise RuntimeError("M6 tutoring decision payload is invalid") from error
    if record.evidence_fingerprint != str(row["evidence_fingerprint"]):
        raise RuntimeError("M6 evidence fingerprint does not match its payload")
    return record


def _require_canonical_json(payload: str, entity: str) -> None:
    try:
        value: Any = json.loads(payload)
        canonical = dumps_json(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"M6 {entity} JSON is invalid") from error
    if canonical != payload:
        raise RuntimeError(f"M6 {entity} JSON is not canonical")


def _assert_same_decision(
    stored: TutoringDecisionRecord,
    candidate: TutoringDecisionRecord,
) -> None:
    if (
        stored.decision_id != candidate.decision_id
        or stored.session_id != candidate.session_id
        or stored.turn_count != candidate.turn_count
        or stored.previous_turn_count != candidate.previous_turn_count
        or stored.request_fingerprint != candidate.request_fingerprint
        or stored.input_fingerprint != candidate.input_fingerprint
        or stored.evidence_fingerprint != candidate.evidence_fingerprint
        or stored.result.content_checksum() != candidate.result.content_checksum()
    ):
        raise RuntimeError("M6 stored decision does not match its candidate")
