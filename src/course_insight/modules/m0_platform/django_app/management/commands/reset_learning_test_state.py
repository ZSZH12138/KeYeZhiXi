"""Back up and clear course learning data while preserving identities and APIs."""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
    ClassLearningSnapshot,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeChangeOperation,
    KnowledgeIngestionJob,
    LearnerConceptMastery,
    LearningProfileProjectionEvent,
    TeacherItemReviewNote,
    WrongQuestionRecord,
)


_CONFIRMATION = "RESET-LEARNING-TEST-STATE"
_LEARNING_TABLE_DELETE_ORDER = (
    "m0_event_outbox",
    "m0_learning_events",
    "m0_assessment_runs",
    "m9_model_invocation_audits",
    "m9_teacher_reviews",
    "m9_teacher_analytics",
    "m8_score_audits",
    "m8_scoring_results",
    "m8_frozen_assessment_records",
    "m8_assessment_papers",
    "m8_adaptive_selections",
    "m8_ability_estimates",
    "m8_calibration_reviews",
    "m8_irt_calibration_runs",
    "m8_irt_parameter_sets",
    "m7_model_invocation_audits",
    "m7_student_feedback",
    "m6_policy_observations",
    "m6_policy_rewards",
    "m6_tutoring_decisions",
    "m6_policy_executions",
    "m6_session_states",
    "m5_learning_observation_audits",
    "m5_learning_observations",
    "m5_state_updates",
    "m5_knowledge_traces",
    "m5_dina_models",
    "m5_bkt_models",
    "m5_class_states",
    "m5_learner_states",
    "m4_intent_decisions",
    "m4_task_plans",
)


class Command(BaseCommand):
    help = (
        "Back up then clear uploaded course files, teacher questions, "
        "assessment history, wrong questions, and learner profiles."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--confirm", required=True)
        parser.add_argument("--backup-dir")

    def handle(self, *args, **options) -> None:
        del args
        if options["confirm"] != _CONFIRMATION:
            raise CommandError(
                f"refusing reset: --confirm must equal {_CONFIRMATION}"
            )
        if connection.vendor != "sqlite":
            raise CommandError(
                "this reset command currently requires the configured SQLite runtime"
            )
        runtime_root = _safe_directory(Path(settings.COURSE_INSIGHT_RUNTIME_DIR))
        upload_root = _safe_directory(
            Path(settings.COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR)
        )
        frozen_root = _safe_directory(runtime_root / "frozen_submissions")
        state_path = Path(settings.COURSE_INSIGHT_STATE_POLICY_PATH).resolve()
        correction_path = state_path.parent / "correction_records.json"
        backup_base = _safe_directory(
            Path(options["backup_dir"]).resolve()
            if options.get("backup_dir")
            else runtime_root / "backups"
        )
        backup_dir = _new_backup_directory(backup_base)

        _backup_sqlite(backup_dir / "course_insight-before-reset.sqlite3")
        _archive_directory(upload_root, backup_dir / "knowledge_uploads.zip")
        _archive_directory(frozen_root, backup_dir / "frozen_submissions.zip")
        if correction_path.is_file():
            shutil.copy2(correction_path, backup_dir / "correction_records.json")
        else:
            (backup_dir / "correction_records.json").write_text(
                "{}\n", encoding="utf-8"
            )

        counts = _clear_database()
        _clear_directory(upload_root)
        _clear_directory(frozen_root)
        correction_path.parent.mkdir(parents=True, exist_ok=True)
        correction_path.write_text("{}\n", encoding="utf-8")

        summary = {
            "backup_dir": str(backup_dir),
            "preserved": [
                "users",
                "actor_grants",
                "groups_and_permissions",
                "course_class_workspaces",
                "scoped_deepseek_configurations",
                "bootstrap_runtime_artifacts",
            ],
            "cleared": counts,
        }
        self.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _safe_directory(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
        raise CommandError(f"unsafe reset directory: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _new_backup_directory(base: Path) -> Path:
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S-%f")
    target = (base / f"learning-reset-{stamp}").resolve()
    if target.parent != base.resolve():
        raise CommandError("backup directory escaped its configured parent")
    target.mkdir(parents=False, exist_ok=False)
    return target


def _backup_sqlite(destination: Path) -> None:
    connection.ensure_connection()
    source = connection.connection
    if source is None:
        raise CommandError("SQLite connection is unavailable for backup")
    with sqlite3.connect(destination) as backup:
        source.backup(backup)


def _archive_directory(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source))


@transaction.atomic
def _clear_database() -> dict[str, int]:
    counts = {
        "assessment_projection_receipts": AssessmentProjectionReceipt.objects.count(),
        "learner_masteries": LearnerConceptMastery.objects.count(),
        "class_learning_snapshots": ClassLearningSnapshot.objects.count(),
        "learning_profile_projection_events": (
            LearningProfileProjectionEvent.objects.count()
        ),
        "teacher_item_review_notes": TeacherItemReviewNote.objects.count(),
        "wrong_questions": WrongQuestionRecord.objects.count(),
        "course_sources": CourseSource.objects.count(),
        "knowledge_releases": CourseKnowledgeRelease.objects.count(),
    }
    AssessmentProjectionReceipt.objects.all().delete()
    TeacherItemReviewNote.objects.all().delete()
    LearningProfileProjectionEvent.objects.all().delete()
    ClassLearningSnapshot.objects.all().delete()
    LearnerConceptMastery.objects.all().delete()
    WrongQuestionRecord.objects.all().delete()
    CourseClassWorkspace.objects.update(
        active_release=None,
        content_revision=F("content_revision") + 1,
    )
    CourseKnowledgeRelease.objects.all().delete()
    KnowledgeChangeOperation.objects.all().delete()
    KnowledgeIngestionJob.objects.all().delete()
    CourseSourceVersion.objects.all().delete()
    CourseSource.objects.all().delete()

    existing = frozenset(connection.introspection.table_names())
    quoted = connection.ops.quote_name
    with connection.cursor() as cursor:
        for table_name in _LEARNING_TABLE_DELETE_ORDER:
            if table_name in existing:
                cursor.execute(f"DELETE FROM {quoted(table_name)}")
    return counts


def _clear_directory(root: Path) -> None:
    resolved_root = root.resolve()
    for child in tuple(resolved_root.iterdir()):
        resolved_child = child.resolve()
        if resolved_child.parent != resolved_root:
            raise CommandError(f"reset target escaped configured root: {child}")
        if resolved_child.is_dir():
            shutil.rmtree(resolved_child)
        else:
            resolved_child.unlink()
