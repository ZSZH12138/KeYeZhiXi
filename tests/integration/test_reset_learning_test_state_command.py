from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import override_settings

from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    ClassLearningSnapshot,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    LearnerConceptMastery,
    LearningProfileProjectionEvent,
    ScopedDeepSeekConfiguration,
    TeacherItemReviewNote,
    User,
    WrongQuestionRecord,
)
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    save_scoped_deepseek_settings,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    synchronize_class_learning_snapshot,
)
from tests.integration.test_django_student_qa import _active_release


pytestmark = pytest.mark.django_db(transaction=True)


def test_reset_preserves_bootstrap_runtime_artifacts(tmp_path: Path) -> None:
    """Clearing test data must not make the Web runtime unrestorable."""

    fixtures = {
        "m1_course_packages": (
            ("course_package_id", "package_version", "payload"),
            ("package_reset_guard", "1.0.0", "{}"),
        ),
        "m2_evidence_indexes": (
            ("index_id", "index_version", "payload"),
            ("index_reset_guard", "1.0.0", "{}"),
        ),
        "m3_knowledge_bundles": (
            ("knowledge_bundle_id", "bundle_version", "payload"),
            ("bundle_reset_guard", "1.0.0", "{}"),
        ),
        "s1_s6_artifacts": (
            (
                "module",
                "object_type",
                "object_id",
                "object_version",
                "status",
                "content_checksum",
                "payload_version",
                "payload_checksum",
                "payload",
                "created_at",
            ),
            (
                "m3",
                "knowledge_bundle",
                "bundle_reset_guard",
                "1.0.0",
                "published",
                "a" * 64,
                "1",
                "b" * 64,
                "{}",
                "2026-08-26T00:00:00+00:00",
            ),
        ),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE m1_course_packages ("
            "course_package_id TEXT NOT NULL, package_version TEXT NOT NULL, "
            "payload TEXT NOT NULL, "
            "PRIMARY KEY (course_package_id, package_version))"
        )
        cursor.execute(
            "CREATE TABLE m2_evidence_indexes ("
            "index_id TEXT NOT NULL, index_version TEXT NOT NULL, "
            "payload TEXT NOT NULL, PRIMARY KEY (index_id, index_version))"
        )
        cursor.execute(
            "CREATE TABLE m3_knowledge_bundles ("
            "knowledge_bundle_id TEXT NOT NULL, bundle_version TEXT NOT NULL, "
            "payload TEXT NOT NULL, "
            "PRIMARY KEY (knowledge_bundle_id, bundle_version))"
        )
        cursor.execute(
            "CREATE TABLE s1_s6_artifacts ("
            "module TEXT NOT NULL, object_type TEXT NOT NULL, "
            "object_id TEXT NOT NULL, object_version TEXT NOT NULL, "
            "status TEXT NOT NULL, content_checksum TEXT NOT NULL, "
            "payload_version TEXT NOT NULL, payload_checksum TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (module, object_type, object_id, object_version))"
        )
        for table_name, (columns, values) in fixtures.items():
            placeholders = ", ".join(["%s"] * len(values))
            cursor.execute(
                f"INSERT INTO {table_name} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                values,
            )

    with override_settings(
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "uploads",
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_STATE_POLICY_PATH=tmp_path / "state.json",
    ):
        call_command(
            "reset_learning_test_state",
            confirm="RESET-LEARNING-TEST-STATE",
            backup_dir=str(tmp_path / "backups"),
        )

    with connection.cursor() as cursor:
        for table_name in fixtures:
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            assert cursor.fetchone() == (1,)


def test_reset_backs_up_then_clears_learning_state_but_preserves_accounts_and_api(
    tmp_path: Path,
) -> None:
    actor_id = "pseudonym_reset_student"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    ActorGrant.objects.create(
        user=user,
        role="student",
        course_id="course_1",
        class_id="class_1",
        source_checksum="a" * 64,
    )
    _active_release(user, example_count=1)
    workspace = CourseClassWorkspace.objects.get()
    save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-reset-preserved",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=user,
    )
    LearnerConceptMastery.objects.create(
        workspace=workspace,
        learner=user,
        concept_id="concept-1",
        attempted_count=1,
        correct_count=0,
        attempt_status="attempted",
        mastery=0,
    )
    snapshot = synchronize_class_learning_snapshot(
        workspace=workspace,
        reason="test",
    )
    persisted_snapshot = ClassLearningSnapshot.objects.get(
        snapshot_id=snapshot.snapshot_id,
    )
    LearningProfileProjectionEvent.objects.create(
        workspace=workspace,
        learner=user,
        attempt_id="attempt-reset-event",
        task_type="diagnostic",
        scoring_checksum="b" * 64,
        changed_concept_ids=["concept-1"],
        reason="test",
        snapshot=persisted_snapshot,
    )
    TeacherItemReviewNote.objects.create(
        workspace=workspace,
        learner=user,
        reviewed_by=user,
        attempt_id="attempt-reset-note",
        paper_id="paper-reset-note",
        item_instance_id="item-reset-note",
        audit_id="audit-reset-note",
        audit_version=1,
        score=Decimal("0"),
        max_score=Decimal("1"),
        teacher_note="待删除的教师备注",
    )
    WrongQuestionRecord.objects.create(
        workspace=workspace,
        learner=user,
        item_id="q-1",
        item_version="v1",
        latest_attempt_id="attempt-1",
    )
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    (upload_root / "opaque-file").write_text("course bytes", encoding="utf-8")
    frozen_root = tmp_path / "frozen_submissions"
    frozen_root.mkdir()
    (frozen_root / "attempt.json").write_text("{}", encoding="utf-8")
    correction_path = tmp_path / "correction_records.json"
    correction_path.write_text('{"paper": {"items": {}}}', encoding="utf-8")
    backup_root = tmp_path / "backups"

    with override_settings(
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=upload_root,
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_STATE_POLICY_PATH=tmp_path / "state.json",
    ):
        call_command(
            "reset_learning_test_state",
            confirm="RESET-LEARNING-TEST-STATE",
            backup_dir=str(backup_root),
        )

    assert User.objects.filter(pk=user.pk).exists()
    assert ActorGrant.objects.filter(user=user).exists()
    assert ScopedDeepSeekConfiguration.objects.filter(workspace=workspace).exists()
    workspace.refresh_from_db()
    assert workspace.active_release_id is None
    assert not CourseSource.objects.exists()
    assert not CourseKnowledgeRelease.objects.exists()
    assert not LearnerConceptMastery.objects.exists()
    assert not ClassLearningSnapshot.objects.exists()
    assert not LearningProfileProjectionEvent.objects.exists()
    assert not TeacherItemReviewNote.objects.exists()
    assert not WrongQuestionRecord.objects.exists()
    assert not any(upload_root.iterdir())
    assert not any(frozen_root.iterdir())
    assert json.loads(correction_path.read_text(encoding="utf-8")) == {}
    created = tuple(backup_root.glob("learning-reset-*"))
    assert len(created) == 1
    assert (created[0] / "course_insight-before-reset.sqlite3").is_file()
    assert (created[0] / "knowledge_uploads.zip").is_file()
    assert (created[0] / "frozen_submissions.zip").is_file()
    assert (created[0] / "correction_records.json").is_file()
