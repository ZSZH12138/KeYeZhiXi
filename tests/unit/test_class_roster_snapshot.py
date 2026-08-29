from datetime import datetime, timezone

import pytest

from course_insight.application.class_roster import ClassRosterSnapshot
from course_insight.contracts.errors import DomainError


NOW = datetime(2026, 8, 29, 1, 30, tzinfo=timezone.utc)


def test_roster_snapshot_sorts_deduplicates_and_hashes_pseudonyms() -> None:
    snapshot = ClassRosterSnapshot.capture(
        course_id="course_1",
        class_id="class_1",
        learner_ids=["pseudonym_b", "pseudonym_a", "pseudonym_b"],
        captured_at=NOW,
    )

    assert snapshot.learner_ids == ("pseudonym_a", "pseudonym_b")
    assert snapshot.active_student_count == 2
    assert snapshot.contains("pseudonym_a")
    assert len(snapshot.roster_checksum) == 64


def test_roster_snapshot_rejects_naive_time_and_blank_scope() -> None:
    with pytest.raises(DomainError) as naive:
        ClassRosterSnapshot.capture(
            course_id="course_1",
            class_id="class_1",
            learner_ids=[],
            captured_at=datetime(2026, 8, 29),
        )
    with pytest.raises(DomainError) as blank:
        ClassRosterSnapshot.capture(
            course_id=" ",
            class_id="class_1",
            learner_ids=[],
            captured_at=NOW,
        )

    assert naive.value.code == "CLASS_ROSTER_INVALID"
    assert blank.value.code == "CLASS_ROSTER_INVALID"
