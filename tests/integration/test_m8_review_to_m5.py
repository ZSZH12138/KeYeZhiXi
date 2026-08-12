"""Teacher review must remain atomic and govern downstream M5 evidence."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from course_insight.application.coordinator import AppCoordinator
from course_insight.contracts.analytics import (
    CriterionOverride,
    TeacherReviewDecision,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.platform import TeacherReviewSubmission
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.infrastructure.sqlite.m9_repository import SQLiteM9Repository
from course_insight.modules.m5_learner_class_state.learning_history import (
    build_complete_history_batch,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from course_insight.modules.m8_assessment_scoring.service import (
    M8AssessmentService,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from tests.factories.m5_m8 import UTC_TIME, make_paper, make_scoring_bundle


def _runtime(database_path: Path):
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    paper = make_paper(subjective=False)
    record = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )
    repository.insert_or_get_paper_record(record)
    original = repository.insert_or_get_scoring_result(
        make_scoring_bundle(paper)
    )
    service = M8AssessmentService(
        repository=repository,
        rule_scorer=object(),
        parameter_item_generator=object(),
    )
    return repository, service, original


def _decision(
    original,
    *,
    decision_id: str,
    decision: str = "confirm",
    expected_checksum: str | None = None,
) -> TeacherReviewDecision:
    audit = original.score_audit_records[0]
    is_override = decision == "override"
    final_score = 0.0 if is_override else audit.total_score
    return TeacherReviewDecision(
        decision_id=decision_id,
        audit_id=audit.audit_id,
        expected_audit_version=audit.audit_version,
        expected_audit_checksum=(
            audit.content_checksum()
            if expected_checksum is None
            else expected_checksum
        ),
        decision=decision,
        final_total_score=final_score,
        criterion_overrides=(
            [
                CriterionOverride(
                    criterion_id=audit.criterion_scores[0].criterion_id,
                    previous_score=audit.criterion_scores[0].score,
                    new_score=0.0,
                    reason="Teacher corrected the score.",
                )
            ]
            if is_override
            else []
        ),
        teacher_comment=f"Teacher chose {decision}.",
        reviewer_id="teacher_1",
        reviewed_at=UTC_TIME + timedelta(minutes=10),
    )


def test_teacher_review_rejects_stale_audit_checksum(tmp_path: Path) -> None:
    """A matching version number is insufficient after evidence changes."""

    _, service, original = _runtime(tmp_path / "checksum-conflict.sqlite3")
    stale = _decision(
        original,
        decision_id="review_stale_checksum",
        expected_checksum="0" * 64,
    )

    with pytest.raises(DomainError) as error:
        service.apply_teacher_review(original, stale)

    assert error.value.code == "REVIEW_VERSION_CONFLICT"


def test_teacher_override_is_atomic_append_only_and_replay_safe(
    tmp_path: Path,
) -> None:
    """Only one v2 winner may follow v1, and the same request may replay."""

    repository, service, original = _runtime(tmp_path / "atomic-review.sqlite3")
    decision = _decision(
        original,
        decision_id="review_override",
        decision="override",
    )

    first = service.apply_teacher_review(original, decision)
    replay = service.apply_teacher_review(original, decision.model_copy(deep=True))

    original_audit = original.score_audit_records[0]
    reviewed_audit = first.get_audit_record(original_audit.audit_id)
    assert replay == first
    assert reviewed_audit.audit_version == 2
    assert reviewed_audit.scoring_method == "teacher_override"
    assert reviewed_audit.review_status == "approved"
    assert repository.get_score_audit(original_audit.audit_id, 1) == original_audit
    assert repository.get_score_audit(original_audit.audit_id, 2) == reviewed_audit

    conflicting = _decision(
        original,
        decision_id="review_conflicting_winner",
        decision="confirm",
    )
    with pytest.raises(DomainError) as error:
        service.apply_teacher_review(original, conflicting)
    assert error.value.code == "REVIEW_VERSION_CONFLICT"


def test_second_teacher_decision_for_same_version_reports_review_conflict(
    tmp_path: Path,
) -> None:
    """The review ledger must expose a stable conflict, not a generic failure."""

    _, _, original = _runtime(tmp_path / "review-ledger-m8.sqlite3")
    repository = SQLiteM9Repository(tmp_path / "review-ledger-m9.sqlite3")
    repository.initialize()
    service = M9TeacherAnalyticsService(repository, object(), object())
    audit = original.score_audit_records[0]

    def submission(decision_id: str, decision: str) -> TeacherReviewSubmission:
        return TeacherReviewSubmission(
            submission_id=decision_id,
            audit_id=audit.audit_id,
            expected_audit_version=audit.audit_version,
            expected_audit_checksum=audit.content_checksum(),
            reviewer_id="teacher_1",
            decision=decision,
            final_total_score=audit.total_score,
            criterion_overrides=[],
            teacher_comment=f"Teacher chose {decision}.",
            submitted_at=UTC_TIME + timedelta(minutes=5),
        )

    service.record_teacher_review(
        raw_review_path=submission("decision_first", "confirm"),
        current_scoring_result_bundle=original,
    )
    with pytest.raises(DomainError) as error:
        service.record_teacher_review(
            raw_review_path=submission("decision_second", "reject"),
            current_scoring_result_bundle=original,
        )

    assert error.value.code == "REVIEW_VERSION_CONFLICT"


def test_concurrent_m8_reviews_have_one_winner_and_one_stable_conflict(
    tmp_path: Path,
) -> None:
    """The database lock must serialize two teachers reviewing the same v1."""

    repository, _, original = _runtime(tmp_path / "concurrent-review.sqlite3")
    decisions = (
        _decision(
            original,
            decision_id="concurrent_override",
            decision="override",
        ),
        _decision(
            original,
            decision_id="concurrent_confirm",
            decision="confirm",
        ),
    )
    barrier = Barrier(2)
    lock = Lock()
    winners = []
    error_codes: list[str] = []

    def review(decision: TeacherReviewDecision) -> None:
        service = M8AssessmentService(repository, object(), object())
        barrier.wait()
        try:
            result = service.apply_teacher_review(original, decision)
            with lock:
                winners.append(result)
        except DomainError as error:
            with lock:
                error_codes.append(error.code)

    threads = [Thread(target=review, args=(decision,)) for decision in decisions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert error_codes == ["REVIEW_VERSION_CONFLICT"]
    assert (
        repository.get_scoring_result(original.attempt_id)
        == winners[0]
    )


def test_rejected_review_records_reason_and_produces_no_m5_observation(
    tmp_path: Path,
) -> None:
    """Rejected evidence remains auditable but must not change learner state."""

    _, service, original = _runtime(tmp_path / "rejected-review.sqlite3")
    decision = _decision(
        original,
        decision_id="review_reject",
        decision="reject",
    )

    reviewed = service.apply_teacher_review(original, decision)
    audit = reviewed.get_audit_record(decision.audit_id)
    observations = service.build_observation_batch(reviewed.paper_id, reviewed)

    assert audit.review_status == "rejected"
    assert audit.review_reason == [decision.teacher_comment]
    assert observations.observations == []


def test_confirmed_review_produces_only_the_new_audit_version(
    tmp_path: Path,
) -> None:
    """Persist history, but prevent M5 from counting v1 and v2 together."""

    _, service, original = _runtime(tmp_path / "confirmed-review.sqlite3")
    decision = _decision(original, decision_id="review_confirm")
    reviewed = service.apply_teacher_review(original, decision)
    original_batch = service.build_observation_batch(original.paper_id, original)
    reviewed_batch = service.build_observation_batch(reviewed.paper_id, reviewed)

    m5_repository = SQLiteM5Repository(tmp_path / "m5-history.sqlite3")
    m5_repository.initialize()
    m5_repository.insert_or_get_learning_observation_batch(original_batch)
    m5_repository.insert_or_get_learning_observation_batch(reviewed_batch)
    history = build_complete_history_batch(
        m5_repository,
        reviewed_batch,
        course_id="course_1",
        class_id="class_1",
    )

    assert len(history.observations) == 1
    assert history.observations[0].source_audit_id == decision.audit_id
    assert history.observations[0].source_audit_version == 2


def test_legacy_rejected_review_does_not_call_m5_state_update(
    tmp_path: Path,
) -> None:
    """The compatibility workflow must preserve the prior learner state."""

    _, service, original = _runtime(tmp_path / "legacy-reject.sqlite3")
    decision = _decision(
        original,
        decision_id="legacy_review_reject",
        decision="reject",
    )

    class M9:
        def record_teacher_review(self, **_):
            return decision

        def build_teacher_analytics(self, **_):
            return decision

    class M0:
        def append_learning_events(self, **_):
            return None

    class M5:
        def update_state(self, **_):
            raise AssertionError("rejected reviews must not update M5")

        def run_learning_models(self, *_):
            raise AssertionError("rejected reviews must not run M5 models")

    coordinator = object.__new__(AppCoordinator)
    coordinator._m0 = M0()
    coordinator._m5 = M5()
    coordinator._m8 = service
    coordinator._m9 = M9()

    result = coordinator.run_teacher_review_cycle(
        knowledge_bundle=decision,
        scoring_result_bundle=original,
        state_update_result=decision,
        raw_review_path=Path("unused.json"),
        state_policy_path=Path("unused-state.json"),
        teacher_threshold_policy_path=Path("unused-teacher.json"),
    )

    assert result["recomputed_state_result"] == decision
    assert (
        result["reviewed_scoring_result"]
        .get_audit_record(decision.audit_id)
        .review_status
        == "rejected"
    )
