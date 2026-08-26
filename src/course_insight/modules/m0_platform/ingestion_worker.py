"""Database-claimed knowledge-ingestion worker."""

from __future__ import annotations

import uuid
import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app.models import KnowledgeIngestionJob


class KnowledgeIngestionWorker:
    """Claim queued jobs with a finite lease and process one at a time."""

    def __init__(
        self,
        processor: Any | None = None,
        *,
        processor_factory: Callable[[], Any] | None = None,
        worker_id: str | None = None,
    ) -> None:
        if (processor is None) == (processor_factory is None):
            raise ValueError("provide exactly one processor or processor_factory")
        self._processor = processor
        self._processor_factory = processor_factory
        self.worker_id = worker_id or f"ingestion-{uuid.uuid4()}"

    def run_once(self) -> bool:
        with transaction.atomic():
            now = timezone.now()
            job = (
                KnowledgeIngestionJob.objects.select_for_update(skip_locked=True)
                .filter(
                    Q(status=KnowledgeIngestionJob.Status.QUEUED)
                    | Q(
                        status=KnowledgeIngestionJob.Status.RUNNING,
                        lease_until__lt=now,
                    )
                    | Q(
                        status=KnowledgeIngestionJob.Status.RUNNING,
                        lease_until__isnull=True,
                    )
                )
                .order_by("created_at")
                .first()
            )
            if job is None:
                return False
            job.status = KnowledgeIngestionJob.Status.RUNNING
            job.progress = 1
            job.started_at = job.started_at or now
            job.worker_id = self.worker_id
            job.lease_until = now + timedelta(minutes=5)
            job.save(
                update_fields=(
                    "status",
                    "progress",
                    "started_at",
                    "worker_id",
                    "lease_until",
                    "updated_at",
                )
            )
        processor = self._processor
        if self._processor_factory is not None:
            try:
                parameters = tuple(
                    inspect.signature(
                        self._processor_factory
                    ).parameters.values()
                )
            except (TypeError, ValueError):
                parameters = ()
            accepts_job = any(
                parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                }
                for parameter in parameters
            )
            processor = (
                self._processor_factory(job)
                if accepts_job
                else self._processor_factory()
            )
        with bind_log_context(
            job_id=str(job.pk),
            course_id=job.course_id,
            class_id=job.class_id,
            worker_id=self.worker_id,
        ):
            processor.process(job.pk)
        return True
