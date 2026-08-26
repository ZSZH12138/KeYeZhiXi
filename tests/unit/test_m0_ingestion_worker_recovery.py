from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from course_insight.infrastructure.log_context import current_log_context
from course_insight.modules.m0_platform.django_app.models import (
    KnowledgeIngestionJob,
    User,
)
from course_insight.modules.m0_platform.ingestion_worker import KnowledgeIngestionWorker


pytestmark = pytest.mark.django_db


class _RecordingProcessor:
    def __init__(self) -> None:
        self.job_ids = []

    def process(self, job_id) -> None:
        self.job_ids.append(job_id)


def test_worker_reclaims_running_job_after_lease_expires() -> None:
    user = User.objects.create_user(
        username="pseudonym_recovery_teacher",
        actor_id="pseudonym_recovery_teacher",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=user,
        change_set_checksum=hashlib.sha256(b"stale-running-job").hexdigest(),
        status=KnowledgeIngestionJob.Status.RUNNING,
        progress=42,
        worker_id="dead-worker",
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    processor = _RecordingProcessor()

    processed = KnowledgeIngestionWorker(
        processor,
        worker_id="replacement-worker",
    ).run_once()

    assert processed is True
    assert processor.job_ids == [job.pk]
    job.refresh_from_db()
    assert job.status == KnowledgeIngestionJob.Status.RUNNING
    assert job.worker_id == "replacement-worker"
    assert job.lease_until is not None
    assert job.lease_until > timezone.now()


def test_worker_builds_a_fresh_processor_for_each_claimed_job() -> None:
    user = User.objects.create_user(
        username="pseudonym_fresh_config_teacher",
        actor_id="pseudonym_fresh_config_teacher",
    )
    jobs = [
        KnowledgeIngestionJob.objects.create(
            course_id="course_1",
            requested_by=user,
            change_set_checksum=hashlib.sha256(marker).hexdigest(),
        )
        for marker in (b"fresh-config-1", b"fresh-config-2")
    ]
    processors: list[_RecordingProcessor] = []

    def processor_factory() -> _RecordingProcessor:
        processor = _RecordingProcessor()
        processors.append(processor)
        return processor

    worker = KnowledgeIngestionWorker(
        processor_factory=processor_factory,
        worker_id="fresh-config-worker",
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert len(processors) == 2
    assert processors[0].job_ids == [jobs[0].pk]
    assert processors[1].job_ids == [jobs[1].pk]


def test_worker_passes_claimed_course_class_to_processor_factory() -> None:
    user = User.objects.create_user(
        username="pseudonym_scoped_worker_teacher",
        actor_id="pseudonym_scoped_worker_teacher",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        class_id="class_2",
        requested_by=user,
        change_set_checksum=hashlib.sha256(b"scoped-worker").hexdigest(),
    )
    seen: list[tuple[str, str]] = []

    def processor_factory(claimed_job) -> _RecordingProcessor:
        seen.append((claimed_job.course_id, claimed_job.class_id))
        return _RecordingProcessor()

    worker = KnowledgeIngestionWorker(
        processor_factory=processor_factory,
        worker_id="scoped-worker",
    )

    assert worker.run_once() is True
    assert seen == [("course_1", "class_2")]
    assert KnowledgeIngestionJob.objects.get(pk=job.pk).worker_id == (
        "scoped-worker"
    )


def test_worker_binds_job_scope_to_processing_logs() -> None:
    user = User.objects.create_user(
        username="pseudonym_ingestion_log_context_teacher",
        actor_id="pseudonym_ingestion_log_context_teacher",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        class_id="class_2",
        requested_by=user,
        change_set_checksum=hashlib.sha256(b"ingestion-log-context").hexdigest(),
    )
    observed = []

    class _ContextProcessor:
        def process(self, job_id) -> None:
            assert job_id == job.pk
            observed.append(current_log_context())

    worker = KnowledgeIngestionWorker(
        _ContextProcessor(),
        worker_id="ingestion-log-worker",
    )

    assert worker.run_once() is True
    assert observed[0].job_id == str(job.pk)
    assert observed[0].course_id == "course_1"
    assert observed[0].class_id == "class_2"
    assert observed[0].worker_id == "ingestion-log-worker"
