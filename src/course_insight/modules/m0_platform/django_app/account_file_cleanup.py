"""Safe, retryable physical file cleanup after account erasure commits."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

from django.conf import settings
from django.db.models import F

from course_insight.modules.m0_platform.django_app.models import (
    ErasureFileCleanup,
)


logger = logging.getLogger(__name__)
_STORAGE_KEY = re.compile(r"^[0-9a-f]{32}/[0-9a-f-]{36}$")
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_BATCH_SIZE = 1_000


def queue_erasure_file_cleanup(
    *,
    storage_keys: Iterable[str],
    frozen_attempt_ids: Iterable[str],
) -> None:
    """Persist idempotent cleanup work before the related rows are deleted."""

    jobs: list[ErasureFileCleanup] = []
    for storage_key in sorted(set(storage_keys)):
        if not isinstance(storage_key, str) or not _STORAGE_KEY.fullmatch(storage_key):
            raise ValueError("knowledge storage key is invalid")
        jobs.append(
            ErasureFileCleanup(
                kind=ErasureFileCleanup.Kind.KNOWLEDGE_UPLOAD,
                opaque_key=storage_key,
            )
        )
    for attempt_id in sorted(set(frozen_attempt_ids)):
        if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
            raise ValueError("frozen submission identifier is invalid")
        jobs.append(
            ErasureFileCleanup(
                kind=ErasureFileCleanup.Kind.FROZEN_SUBMISSION,
                opaque_key=attempt_id,
            )
        )
    if jobs:
        ErasureFileCleanup.objects.bulk_create(jobs, ignore_conflicts=True)


def purge_pending_erasure_files(*, limit: int = _MAX_BATCH_SIZE) -> int:
    """Delete one bounded batch of committed cleanup jobs.

    A failed unlink does not resurrect deleted account metadata.  The opaque
    key remains in the queue for the next invocation and no filesystem path is
    logged.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_BATCH_SIZE:
        raise ValueError("cleanup batch size is invalid")
    jobs = list(
        ErasureFileCleanup.objects.order_by("queued_at", "pk")[:limit]
    )
    deleted = 0
    for job in jobs:
        try:
            target = _target_for(job)
            if target.exists() and not target.is_file():
                raise OSError("cleanup target is not a file")
            target.unlink(missing_ok=True)
            _remove_empty_upload_parent(job, target)
        except (OSError, ValueError):
            ErasureFileCleanup.objects.filter(pk=job.pk).update(
                attempt_count=F("attempt_count") + 1,
                last_error_code="file_delete_failed",
            )
            logger.warning(
                "account erasure file cleanup will retry",
                extra={"file_kind": job.kind},
            )
        else:
            ErasureFileCleanup.objects.filter(pk=job.pk).delete()
            deleted += 1
    return deleted


def _target_for(job: ErasureFileCleanup) -> Path:
    if job.kind == ErasureFileCleanup.Kind.KNOWLEDGE_UPLOAD:
        if not _STORAGE_KEY.fullmatch(job.opaque_key):
            raise ValueError("stored knowledge cleanup key is invalid")
        root = Path(settings.COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR).resolve()
        target = (root / job.opaque_key).resolve()
        if root not in target.parents:
            raise ValueError("knowledge cleanup target escaped its root")
        return target
    if job.kind == ErasureFileCleanup.Kind.FROZEN_SUBMISSION:
        if not _ATTEMPT_ID.fullmatch(job.opaque_key):
            raise ValueError("stored frozen cleanup key is invalid")
        root = (Path(settings.COURSE_INSIGHT_RUNTIME_DIR) / "frozen_submissions").resolve()
        target = (root / f"{job.opaque_key}.json").resolve()
        if target.parent != root:
            raise ValueError("frozen cleanup target escaped its root")
        return target
    raise ValueError("stored cleanup kind is invalid")


def _remove_empty_upload_parent(job: ErasureFileCleanup, target: Path) -> None:
    if job.kind != ErasureFileCleanup.Kind.KNOWLEDGE_UPLOAD:
        return
    root = Path(settings.COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR).resolve()
    parent = target.parent
    if parent.parent != root:
        raise ValueError("knowledge upload parent escaped its root")
    try:
        parent.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        # A directory holding another revision is valid and should be retained.
        return


__all__ = ["purge_pending_erasure_files", "queue_erasure_file_cleanup"]
