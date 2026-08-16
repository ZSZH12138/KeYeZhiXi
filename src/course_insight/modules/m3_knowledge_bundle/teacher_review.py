"""Immutable teacher confirmation workflow for M3 publication."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import dumps_json


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STATES = frozenset({"draft", "submitted", "approved", "rejected", "recalled"})
_MAX_REASON_LENGTH = 2_000
_MAX_PSEUDONYM_LENGTH = 128


ReviewState = Literal["draft", "submitted", "approved", "rejected", "recalled"]


@dataclass(frozen=True, slots=True)
class TeacherReviewAction:
    """One immutable transition record; it contains no raw course content."""

    state: ReviewState
    reviewer_pseudonym: str | None
    reason: str | None
    occurred_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class TeacherReviewRecord:
    """Complete immutable state and transition history for one review."""

    review_id: str
    subject_id: str
    input_checksum: str
    validation_report_ref: str
    state: ReviewState
    version: int
    reviewer_pseudonym: str | None
    reason: str | None
    created_at: datetime
    updated_at: datetime
    history: tuple[TeacherReviewAction, ...]

    def to_payload_text(self) -> str:
        """Encode only canonical, path-free workflow state."""

        return dumps_json(
            {
                "created_at": self.created_at.isoformat(),
                "history": [
                    {
                        "occurred_at": action.occurred_at.isoformat(),
                        "reason": action.reason,
                        "reviewer_pseudonym": action.reviewer_pseudonym,
                        "state": action.state,
                        "version": action.version,
                    }
                    for action in self.history
                ],
                "input_checksum": self.input_checksum,
                "reason": self.reason,
                "review_id": self.review_id,
                "reviewer_pseudonym": self.reviewer_pseudonym,
                "state": self.state,
                "subject_id": self.subject_id,
                "updated_at": self.updated_at.isoformat(),
                "validation_report_ref": self.validation_report_ref,
                "version": self.version,
            }
        )

    @classmethod
    def from_payload_text(cls, payload: str | bytes) -> "TeacherReviewRecord":
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if type(payload) is not bytes:
            raise _invalid_payload()
        try:
            value = json.loads(payload.decode("utf-8"))
            if type(value) is not dict:
                raise ValueError
            history = tuple(
                TeacherReviewAction(
                    state=item["state"],
                    reviewer_pseudonym=item["reviewer_pseudonym"],
                    reason=item["reason"],
                    occurred_at=datetime.fromisoformat(item["occurred_at"]),
                    version=item["version"],
                )
                for item in value["history"]
            )
            record = cls(
                review_id=value["review_id"],
                subject_id=value["subject_id"],
                input_checksum=value["input_checksum"],
                validation_report_ref=value["validation_report_ref"],
                state=value["state"],
                version=value["version"],
                reviewer_pseudonym=value["reviewer_pseudonym"],
                reason=value["reason"],
                created_at=datetime.fromisoformat(value["created_at"]),
                updated_at=datetime.fromisoformat(value["updated_at"]),
                history=history,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise _invalid_payload() from None
        if record.to_payload_text().encode("utf-8") != payload:
            raise _invalid_payload()
        _validate_record(record)
        return record


class TeacherReviewRepository(Protocol):
    """Persistence port for compare-and-swap review records."""

    def get(self, review_id: str) -> TeacherReviewRecord | None:
        """Return a detached immutable review record."""

    def create(self, record: TeacherReviewRecord) -> None:
        """Create one review or reject a conflicting identity."""

    def compare_and_swap(
        self,
        review_id: str,
        expected_version: int,
        record: TeacherReviewRecord,
    ) -> bool:
        """Replace only when the stored version equals expected_version."""


class InMemoryTeacherReviewRepository:
    """Small deterministic adapter used by unit tests and local workflows."""

    def __init__(self) -> None:
        self._records: dict[str, TeacherReviewRecord] = {}

    def get(self, review_id: str) -> TeacherReviewRecord | None:
        return self._records.get(review_id)

    def create(self, record: TeacherReviewRecord) -> None:
        if record.review_id in self._records:
            raise DomainError(
                code="M3_REVIEW_DUPLICATE",
                module="m3",
                message="review identity already exists",
                recoverable=True,
            )
        self._records[record.review_id] = record

    def compare_and_swap(
        self,
        review_id: str,
        expected_version: int,
        record: TeacherReviewRecord,
    ) -> bool:
        current = self._records.get(review_id)
        if current is None or current.version != expected_version:
            return False
        self._records[review_id] = record
        return True


class RepositoryTeacherReviewRepository:
    """Adapt an infrastructure repository's explicit review methods."""

    def __init__(self, repository: object) -> None:
        self._repository = repository
        self._local_lock = threading.RLock()

    @contextmanager
    def lock(self, review_id: str) -> Iterator[None]:
        method = getattr(self._repository, "lock_teacher_review", None)
        if callable(method):
            with method(review_id):
                yield
            return
        with self._local_lock:
            yield

    def get(self, review_id: str) -> TeacherReviewRecord | None:
        method = getattr(self._repository, "get_teacher_review", None)
        if not callable(method):
            raise _repository_unavailable()
        return method(review_id)

    def create(self, record: TeacherReviewRecord) -> None:
        method = getattr(self._repository, "save_teacher_review", None)
        if not callable(method):
            raise _repository_unavailable()
        method(record)

    def compare_and_swap(
        self, review_id: str, expected_version: int, record: TeacherReviewRecord
    ) -> bool:
        method = getattr(self._repository, "compare_and_swap_teacher_review", None)
        if not callable(method):
            raise _repository_unavailable()
        return bool(method(review_id, expected_version, record))


class TeacherReviewWorkflow:
    """Orchestrate the only state transitions allowed before M3 publication."""

    def __init__(self, repository: TeacherReviewRepository) -> None:
        self._repository = repository
        self._local_lock = threading.RLock()

    def create_draft(
        self,
        *,
        review_id: str,
        subject_id: str,
        input_checksum: str,
        validation_report_ref: str,
        now: datetime,
    ) -> TeacherReviewRecord:
        _validate_identity(review_id, subject_id, validation_report_ref)
        _validate_checksum(input_checksum)
        _validate_time(now)
        record = TeacherReviewRecord(
            review_id=review_id,
            subject_id=subject_id,
            input_checksum=input_checksum,
            validation_report_ref=validation_report_ref,
            state="draft",
            version=1,
            reviewer_pseudonym=None,
            reason=None,
            created_at=now,
            updated_at=now,
            history=(
                TeacherReviewAction(
                    state="draft",
                    reviewer_pseudonym=None,
                    reason=None,
                    occurred_at=now,
                    version=1,
                ),
            ),
        )
        try:
            self._repository.create(record)
        except DomainError:
            existing = self._repository.get(review_id)
            if existing == record:
                return existing
            raise
        return record

    def submit(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        return self._transition(
            review_id,
            reviewer_pseudonym=reviewer_pseudonym,
            reason=reason,
            expected_version=expected_version,
            now=now,
            target="submitted",
            allowed={"draft"},
        )

    def approve(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        return self._transition(
            review_id,
            reviewer_pseudonym=reviewer_pseudonym,
            reason=reason,
            expected_version=expected_version,
            now=now,
            target="approved",
            allowed={"submitted"},
        )

    def reject(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        return self._transition(
            review_id,
            reviewer_pseudonym=reviewer_pseudonym,
            reason=reason,
            expected_version=expected_version,
            now=now,
            target="rejected",
            allowed={"submitted"},
        )

    def recall(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        return self._transition(
            review_id,
            reviewer_pseudonym=reviewer_pseudonym,
            reason=reason,
            expected_version=expected_version,
            now=now,
            target="recalled",
            allowed={"approved", "rejected"},
        )

    def require_approved(
        self, review_id: str, expected_version: int | None = None
    ) -> TeacherReviewRecord:
        record = self._get(review_id)
        if record.state != "approved":
            raise DomainError(
                code="M3_REVIEW_NOT_APPROVED",
                module="m3",
                message="review is not approved for publication",
                recoverable=True,
            )
        if expected_version is not None and record.version != expected_version:
            raise DomainError(
                code="M3_REVIEW_VERSION_CONFLICT",
                module="m3",
                message="review version conflict",
                recoverable=True,
            )
        return record

    def publish_approved(
        self,
        review_id: str,
        publisher: Callable[[], object],
        *,
        expected_version: int | None = None,
    ) -> object:
        """Run a publisher only after the current record is approved."""

        with self._review_lock(review_id):
            self.require_approved(review_id, expected_version)
            return publisher()

    @contextmanager
    def _review_lock(self, review_id: str) -> Iterator[None]:
        _validate_identity(review_id)
        method = getattr(self._repository, "lock", None)
        if callable(method):
            with method(review_id):
                yield
            return
        with self._local_lock:
            yield

    def _transition(
        self,
        review_id: str,
        *,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
        target: ReviewState,
        allowed: set[ReviewState],
    ) -> TeacherReviewRecord:
        with self._review_lock(review_id):
            return self._transition_locked(
                review_id,
                reviewer_pseudonym=reviewer_pseudonym,
                reason=reason,
                expected_version=expected_version,
                now=now,
                target=target,
                allowed=allowed,
            )

    def _transition_locked(
        self,
        review_id: str,
        *,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
        target: ReviewState,
        allowed: set[ReviewState],
    ) -> TeacherReviewRecord:
        _validate_reviewer(reviewer_pseudonym)
        _validate_reason(reason)
        _validate_time(now)
        if type(expected_version) is not int or expected_version < 1:
            raise _invalid_payload()
        current = self._get(review_id)
        if current.version != expected_version:
            replay = _find_replay(current, target, reviewer_pseudonym, reason, now)
            if replay is not None:
                return current
            raise DomainError(
                code="M3_REVIEW_VERSION_CONFLICT",
                module="m3",
                message="review version conflict",
                recoverable=True,
            )
        if current.state not in allowed:
            raise DomainError(
                code="M3_REVIEW_INVALID_TRANSITION",
                module="m3",
                message="invalid state transition",
                recoverable=True,
            )
        next_version = current.version + 1
        action = TeacherReviewAction(
            state=target,
            reviewer_pseudonym=reviewer_pseudonym,
            reason=reason,
            occurred_at=now,
            version=next_version,
        )
        updated = replace(
            current,
            state=target,
            version=next_version,
            reviewer_pseudonym=reviewer_pseudonym,
            reason=reason,
            updated_at=now,
            history=(*current.history, action),
        )
        if not self._repository.compare_and_swap(
            review_id, expected_version, updated
        ):
            raise DomainError(
                code="M3_REVIEW_VERSION_CONFLICT",
                module="m3",
                message="review version conflict",
                recoverable=True,
            )
        return updated

    def _get(self, review_id: str) -> TeacherReviewRecord:
        if not isinstance(review_id, str) or not review_id.strip():
            raise _invalid_payload()
        record = self._repository.get(review_id)
        if record is None:
            raise DomainError(
                code="M3_REVIEW_NOT_FOUND",
                module="m3",
                message="review does not exist",
                recoverable=True,
            )
        return record


def review_input_checksum(payload: bytes) -> str:
    """Return the canonical checksum used to bind a review to input bytes."""

    if type(payload) is not bytes:
        raise _invalid_payload()
    return hashlib.sha256(payload).hexdigest()


def _find_replay(
    record: TeacherReviewRecord,
    target: ReviewState,
    reviewer_pseudonym: str,
    reason: str,
    now: datetime,
) -> TeacherReviewAction | None:
    for action in record.history:
        if (
            action.state == target
            and action.reviewer_pseudonym == reviewer_pseudonym
            and action.reason == reason
            and action.occurred_at == now
        ):
            return action
    return None


def _validate_identity(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise _invalid_payload()


def _validate_checksum(value: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _invalid_payload()


def _validate_reviewer(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_PSEUDONYM_LENGTH
        or any(character.isspace() for character in value.strip())
    ):
        raise _invalid_payload()


def _validate_reason(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_REASON_LENGTH
        or "\x00" in value
    ):
        raise _invalid_payload()


def _validate_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invalid_payload()


def _invalid_payload() -> DomainError:
    return DomainError(
        code="M3_REVIEW_INVALID",
        module="m3",
        message="review payload is invalid",
    )


def _validate_record(record: TeacherReviewRecord) -> None:
    _validate_identity(record.review_id, record.subject_id, record.validation_report_ref)
    _validate_checksum(record.input_checksum)
    if record.state not in _STATES or type(record.version) is not int or record.version < 1:
        raise _invalid_payload()
    _validate_time(record.created_at)
    _validate_time(record.updated_at)
    if type(record.history) is not tuple or not record.history:
        raise _invalid_payload()
    if record.history[0].state != "draft" or record.history[0].version != 1:
        raise _invalid_payload()
    if len(record.history) != record.version:
        raise _invalid_payload()
    for expected_version, action in enumerate(record.history, start=1):
        if action.version != expected_version or action.state not in _STATES:
            raise _invalid_payload()
        _validate_time(action.occurred_at)
        if action.reviewer_pseudonym is not None:
            _validate_reviewer(action.reviewer_pseudonym)
        if action.reason is not None:
            _validate_reason(action.reason)
    last = record.history[-1]
    if last.state != record.state or last.version != record.version:
        raise _invalid_payload()


def _repository_unavailable() -> DomainError:
    return DomainError(
        code="M3_REVIEW_PERSISTENCE_FAILED",
        module="m3",
        message="teacher review store is unavailable",
        recoverable=True,
    )


__all__ = [
    "InMemoryTeacherReviewRepository",
    "RepositoryTeacherReviewRepository",
    "TeacherReviewAction",
    "TeacherReviewRecord",
    "TeacherReviewRepository",
    "TeacherReviewWorkflow",
    "review_input_checksum",
]
