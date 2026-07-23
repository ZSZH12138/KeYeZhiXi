"""M6-owned persistence boundary and deterministic in-memory implementation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol

from course_insight.contracts.errors import DomainError
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    derive_identifier,
)


_SESSION_TABLE = "m6_session_states"
_DECISION_TABLE = "m6_tutoring_decisions"


@dataclass(frozen=True, slots=True)
class TutoringDecisionRecord:
    """One immutable authoritative M6 decision and its input identities."""

    decision_id: str
    session_id: str
    turn_count: int
    previous_turn_count: int | None
    request_fingerprint: str
    input_fingerprint: str
    evidence_identity: EvidenceIdentity
    result: TutoringControlResult

    def __post_init__(self) -> None:
        if not self.session_id or self.turn_count < 0:
            raise ValueError("decision session and turn identity are invalid")
        if (
            self.previous_turn_count is not None
            and (
                self.previous_turn_count < 0
                or self.previous_turn_count != self.turn_count - 1
            )
        ):
            raise ValueError("decision previous turn must immediately precede its turn")
        _require_fingerprint(self.request_fingerprint, "request_fingerprint")
        _require_fingerprint(self.input_fingerprint, "input_fingerprint")
        if self.decision_id != derive_identifier("decision", self.input_fingerprint):
            raise ValueError("decision identity must derive from its input fingerprint")
        snapshot = self.result.session_state_snapshot
        if snapshot.session_id != self.session_id or snapshot.turn_count != self.turn_count:
            raise ValueError("decision columns must match the result session snapshot")
        self.result.assert_query_alignment()

    @property
    def evidence_fingerprint(self) -> str:
        """Return the canonical evidence watermark hash."""

        return self.evidence_identity.fingerprint()

    def isolated_copy(self) -> "TutoringDecisionRecord":
        """Return a record whose nested public result is independently rebuilt."""

        result = TutoringControlResult.model_validate(
            self.result.model_dump(mode="python")
        )
        return replace(self, result=result)


class M6Repository(Protocol):
    """Persistence operations owned exclusively by M6."""

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        """Persist one immutable tutoring turn snapshot."""

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        """Load one exact tutoring turn snapshot."""

    def get_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        """Load the latest authoritative snapshot for a session."""

    def get_decision_by_request(
        self,
        request_fingerprint: str,
    ) -> TutoringDecisionRecord | None:
        """Load a prior exact API request result, if any."""

    def get_latest_decision(
        self,
        session_id: str,
    ) -> TutoringDecisionRecord | None:
        """Load the latest evidence-bearing M6 decision for a session."""

    def commit_decision(
        self,
        record: TutoringDecisionRecord,
        expected_previous_snapshot: SessionStateSnapshot,
    ) -> TutoringDecisionRecord:
        """Atomically insert or return one authoritative decision."""


class InMemoryM6Repository:
    """Thread-safe replay semantics for zero-dependency local M6 usage."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._snapshots: dict[tuple[str, int], SessionStateSnapshot] = {}
        self._decisions_by_request: dict[str, TutoringDecisionRecord] = {}
        self._decisions_by_input: dict[str, TutoringDecisionRecord] = {}
        self._decisions_by_turn: dict[tuple[str, int], TutoringDecisionRecord] = {}

    def save_session_state(self, snapshot: SessionStateSnapshot) -> None:
        candidate = isolated_session_snapshot(snapshot)
        key = (candidate.session_id, candidate.turn_count)
        with self._lock:
            existing = self._snapshots.get(key)
            if existing is not None:
                assert_same_session_snapshot(existing, candidate)
                return
            validate_session_append(
                self._latest_snapshot_unlocked(candidate.session_id),
                candidate,
            )
            self._snapshots = {**self._snapshots, key: candidate}

    def get_session_state(
        self,
        session_id: str,
        turn_count: int,
    ) -> SessionStateSnapshot | None:
        with self._lock:
            snapshot = self._snapshots.get((session_id, turn_count))
            return None if snapshot is None else isolated_session_snapshot(snapshot)

    def get_latest_session_state(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        with self._lock:
            candidates = [
                snapshot
                for (stored_session_id, _), snapshot in self._snapshots.items()
                if stored_session_id == session_id
            ]
            if not candidates:
                return None
            latest = max(candidates, key=lambda item: item.turn_count)
            return isolated_session_snapshot(latest)

    def get_decision_by_request(
        self,
        request_fingerprint: str,
    ) -> TutoringDecisionRecord | None:
        with self._lock:
            record = self._decisions_by_request.get(request_fingerprint)
            return None if record is None else record.isolated_copy()

    def get_latest_decision(
        self,
        session_id: str,
    ) -> TutoringDecisionRecord | None:
        with self._lock:
            candidates = [
                record
                for (stored_session_id, _), record in self._decisions_by_turn.items()
                if stored_session_id == session_id
            ]
            if not candidates:
                return None
            latest = max(candidates, key=lambda item: item.turn_count)
            return latest.isolated_copy()

    def commit_decision(
        self,
        record: TutoringDecisionRecord,
        expected_previous_snapshot: SessionStateSnapshot,
    ) -> TutoringDecisionRecord:
        expected = isolated_session_snapshot(expected_previous_snapshot)
        candidate = record.isolated_copy()
        validate_decision_commit(candidate, expected)
        with self._lock:
            replay = self._decisions_by_request.get(candidate.request_fingerprint)
            if replay is not None:
                return replay.isolated_copy()
            replay = self._decisions_by_input.get(candidate.input_fingerprint)
            if replay is not None:
                return replay.isolated_copy()

            latest = self._latest_snapshot_unlocked(candidate.session_id)
            if latest is not None:
                assert_same_session_snapshot(latest, expected)
            result_snapshot = isolated_session_snapshot(
                candidate.result.session_state_snapshot
            )
            result_key = (candidate.session_id, candidate.turn_count)
            existing_result = self._snapshots.get(result_key)
            if existing_result is not None:
                assert_same_session_snapshot(existing_result, result_snapshot)
                _raise_reference_mismatch("decision_turn_already_exists")

            snapshots = dict(self._snapshots)
            if latest is None:
                snapshots[(expected.session_id, expected.turn_count)] = expected
            snapshots[result_key] = result_snapshot
            stored = candidate.isolated_copy()
            self._snapshots = snapshots
            self._decisions_by_request = {
                **self._decisions_by_request,
                stored.request_fingerprint: stored,
            }
            self._decisions_by_input = {
                **self._decisions_by_input,
                stored.input_fingerprint: stored,
            }
            self._decisions_by_turn = {
                **self._decisions_by_turn,
                result_key: stored,
            }
            return stored.isolated_copy()

    def _latest_snapshot_unlocked(
        self,
        session_id: str,
    ) -> SessionStateSnapshot | None:
        candidates = [
            snapshot
            for (stored_session_id, _), snapshot in self._snapshots.items()
            if stored_session_id == session_id
        ]
        return None if not candidates else max(
            candidates,
            key=lambda item: item.turn_count,
        )


def validate_decision_commit(
    record: TutoringDecisionRecord,
    expected: SessionStateSnapshot,
) -> None:
    result_snapshot = record.result.session_state_snapshot
    action = record.result.teaching_action
    if (
        expected.session_id != record.session_id
        or record.previous_turn_count != expected.turn_count
        or record.turn_count != expected.turn_count + 1
        or result_snapshot.completed_action_ids[:-1] != expected.completed_action_ids
        or action.state_before != expected.current_state
    ):
        _raise_reference_mismatch("decision_previous_snapshot_mismatch")


def isolated_session_snapshot(
    snapshot: SessionStateSnapshot,
) -> SessionStateSnapshot:
    return SessionStateSnapshot.model_validate(snapshot.model_dump(mode="python"))


def assert_same_session_snapshot(
    stored: SessionStateSnapshot,
    expected: SessionStateSnapshot,
) -> None:
    if stored.content_checksum() != expected.content_checksum():
        _raise_reference_mismatch("authoritative_session_snapshot_mismatch")


def validate_session_append(
    latest: SessionStateSnapshot | None,
    candidate: SessionStateSnapshot,
) -> None:
    """Reject snapshot writes that do not extend one authoritative FSM history."""

    if latest is None:
        if (
            candidate.turn_count != 0
            or candidate.current_state not in {"S0", "S1"}
        ):
            _raise_reference_mismatch("session_history_has_invalid_start")
        return
    if (
        candidate.session_id != latest.session_id
        or candidate.turn_count != latest.turn_count + 1
        or candidate.completed_action_ids[:-1] != latest.completed_action_ids
    ):
        _raise_reference_mismatch("session_snapshot_not_contiguous")
    if not latest.can_transition_to(candidate.current_state):
        _raise_reference_mismatch("session_snapshot_transition_invalid")


def _require_fingerprint(value: str, field_name: str) -> None:
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _raise_reference_mismatch(reason: str) -> None:
    raise DomainError(
        code="TUTORING_REFERENCE_MISMATCH",
        module="m6",
        message="tutoring history does not match the authoritative session",
        details={"reason": reason},
    )
