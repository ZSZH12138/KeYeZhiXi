"""Back up and normalize assessment history to formal profile evidence."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.utils import timezone

from course_insight.contracts.tasking import PROFILE_AFFECTING_TASK_TYPES
from course_insight.modules.m0_platform.django_app.learning_projection import (
    rebuild_authoritative_profile_state,
)
from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
)
from course_insight.modules.m0_platform.django_app.profile_history_cleanup import (
    cleanup_runtime_history,
)


class Command(BaseCommand):
    help = (
        "Keep only completed diagnostic/stage-assessment history, rebuild "
        "profile counters, and retire the legacy correction JSON store."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply changes after creating a SQLite and legacy-JSON backup.",
        )

    def handle(self, *args, **options) -> None:
        del args
        if connection.vendor != "sqlite":
            raise CommandError("profile history normalization currently requires SQLite")
        database_path = Path(settings.DATABASES["default"]["NAME"]).resolve()
        runtime_root = Path(settings.COURSE_INSIGHT_RUNTIME_DIR).resolve()
        if not database_path.is_file() or runtime_root == Path(runtime_root.anchor):
            raise CommandError("configured runtime paths are unsafe or unavailable")
        keep_receipts = AssessmentProjectionReceipt.objects.filter(
            task_type__in=PROFILE_AFFECTING_TASK_TYPES
        )
        keep_paper_ids = frozenset(keep_receipts.values_list("paper_id", flat=True))
        keep_attempt_ids = frozenset(
            keep_receipts.values_list("attempt_id", flat=True)
        )
        preview = cleanup_runtime_history(
            database_path=database_path,
            keep_paper_ids=keep_paper_ids,
            apply=False,
        )
        if not options["apply"]:
            self.stdout.write(
                json.dumps(
                    {"mode": "preview", **preview},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return

        backup_dir = _create_backup(runtime_root)
        connection.close()
        _backup_sqlite(database_path, backup_dir / database_path.name)
        correction_path = (
            Path(settings.COURSE_INSIGHT_STATE_POLICY_PATH).resolve().parent
            / "correction_records.json"
        )
        if correction_path.is_file():
            shutil.copy2(correction_path, backup_dir / correction_path.name)

        rebuilt = rebuild_authoritative_profile_state()
        connection.close()
        cleanup = cleanup_runtime_history(
            database_path=database_path,
            keep_paper_ids=keep_paper_ids,
            apply=True,
        )
        removed_submissions = _remove_transient_submissions(
            runtime_root / "frozen_submissions",
            keep_attempt_ids=keep_attempt_ids,
        )
        if correction_path.is_file():
            correction_path.unlink()
        self.stdout.write(
            json.dumps(
                {
                    "mode": "applied",
                    "backup_dir": str(backup_dir),
                    "profile_state": rebuilt,
                    "runtime_history": cleanup,
                    "removed_frozen_submissions": removed_submissions,
                    "legacy_correction_json_removed": not correction_path.exists(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )


def _create_backup(runtime_root: Path) -> Path:
    backup_root = (runtime_root / "backups").resolve()
    if backup_root.parent != runtime_root:
        raise CommandError("backup directory escaped the configured runtime")
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%d-%H%M%S-%f")
    target = (backup_root / f"profile-history-{stamp}").resolve()
    if target.parent != backup_root:
        raise CommandError("backup target escaped the configured backup directory")
    target.mkdir(parents=False, exist_ok=False)
    return target


def _backup_sqlite(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as current, sqlite3.connect(destination) as backup:
        current.backup(backup)


def _remove_transient_submissions(
    root: Path,
    *,
    keep_attempt_ids: frozenset[str],
) -> int:
    resolved = root.resolve()
    runtime_root = Path(settings.COURSE_INSIGHT_RUNTIME_DIR).resolve()
    if resolved.parent != runtime_root:
        raise CommandError("frozen submission directory escaped the runtime")
    if not resolved.exists():
        return 0
    removed = 0
    for path in resolved.glob("*.json"):
        candidate = path.resolve()
        if candidate.parent != resolved:
            raise CommandError("frozen submission path escaped its directory")
        if candidate.stem in keep_attempt_ids:
            continue
        candidate.unlink()
        removed += 1
    return removed
