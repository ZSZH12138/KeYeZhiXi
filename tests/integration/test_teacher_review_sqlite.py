from __future__ import annotations

from datetime import datetime, timezone

from course_insight.infrastructure.sqlite.m1_m2_m3_repository import (
    SQLiteM1M2M3Repository,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    RepositoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_sqlite_teacher_review_versions_are_restorable_and_cas_bound(tmp_path) -> None:
    repository = SQLiteM1M2M3Repository(tmp_path / "runtime.sqlite")
    repository.initialize()
    workflow = TeacherReviewWorkflow(RepositoryTeacherReviewRepository(repository))
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="a" * 64,
        validation_report_ref="report-1",
        now=NOW,
    )
    submitted = workflow.submit(
        "review-1", "teacher-1", "checked", draft.version, NOW
    )
    assert repository.get_teacher_review("review-1") == submitted
    assert workflow.approve(
        "review-1", "teacher-1", "approved", submitted.version, NOW
    ).state == "approved"
    assert repository.get_teacher_review("review-1").state == "approved"
    assert workflow.submit("review-1", "teacher-1", "checked", draft.version, NOW).state == "approved"
