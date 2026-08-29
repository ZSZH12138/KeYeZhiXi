"""Immutable identity of one current, exact-scope class roster."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from course_insight.contracts.errors import DomainError


@dataclass(frozen=True, slots=True)
class ClassRosterSnapshot:
    """Server-owned roster input frozen at one profile-changing boundary."""

    course_id: str
    class_id: str
    learner_ids: tuple[str, ...]
    captured_at: datetime
    roster_checksum: str

    def __post_init__(self) -> None:
        canonical_ids = tuple(sorted(set(self.learner_ids)))
        expected_checksum = _roster_checksum(
            self.course_id,
            self.class_id,
            canonical_ids,
        )
        invalid = (
            not _nonblank(self.course_id)
            or not _nonblank(self.class_id)
            or self.learner_ids != canonical_ids
            or any(not _nonblank(learner_id) for learner_id in self.learner_ids)
            or self.captured_at.tzinfo is None
            or self.captured_at.utcoffset() is None
            or not hmac.compare_digest(self.roster_checksum, expected_checksum)
        )
        if invalid:
            raise _invalid_roster()

    @classmethod
    def capture(
        cls,
        *,
        course_id: str,
        class_id: str,
        learner_ids: Iterable[str],
        captured_at: datetime,
    ) -> "ClassRosterSnapshot":
        """Canonicalize pseudonymous learner identities and freeze a checksum."""

        canonical_ids = tuple(sorted(set(learner_ids)))
        return cls(
            course_id=course_id,
            class_id=class_id,
            learner_ids=canonical_ids,
            captured_at=captured_at,
            roster_checksum=_roster_checksum(
                course_id,
                class_id,
                canonical_ids,
            ),
        )

    @property
    def active_student_count(self) -> int:
        return len(self.learner_ids)

    def contains(self, learner_id: str) -> bool:
        return learner_id in self.learner_ids


def _roster_checksum(
    course_id: str,
    class_id: str,
    learner_ids: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "class_id": class_id,
            "course_id": course_id,
            "learner_ids": list(learner_ids),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _invalid_roster() -> DomainError:
    return DomainError(
        code="CLASS_ROSTER_INVALID",
        module="application",
        message="class roster snapshot is invalid",
        recoverable=True,
    )


__all__ = ["ClassRosterSnapshot"]
