from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from course_insight.modules.m0_platform.django_app.models import (
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeIngestionJob,
    ReleaseConcept,
    ReleaseConceptSource,
    User,
)


pytestmark = pytest.mark.django_db


def _user() -> User:
    return User.objects.create_user(
        username="pseudonym_teacher_ingestion",
        actor_id="pseudonym_teacher_ingestion",
    )


def _source_version(user: User) -> tuple[CourseSource, CourseSourceVersion]:
    source = CourseSource.objects.create(
        course_id="course_1",
        display_name="chapter-1.md",
        source_type="knowledge",
        status="staged",
        created_by=user,
    )
    version = CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=f"{uuid.uuid4().hex}/{uuid.uuid4()}",
        sha256="a" * 64,
        media_type="text/markdown",
        size_bytes=128,
        status="staged",
    )
    return source, version


def test_source_version_uses_opaque_storage_key_and_lifecycle() -> None:
    source, version = _source_version(_user())

    assert source.status == "staged"
    assert version.status == "staged"
    assert ":\\" not in version.storage_key and not version.storage_key.startswith("/")

    version.status = "active"
    version.full_clean()
    version.save(update_fields=("status",))
    assert CourseSourceVersion.objects.get(pk=version.pk).status == "active"


def test_only_one_active_release_exists_per_course() -> None:
    user = _user()
    first_job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=user,
        change_set_checksum="a" * 64,
    )
    second_job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=user,
        change_set_checksum="b" * 64,
    )
    CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        version_number=1,
        status="active",
        job=first_job,
        content_checksum="c" * 64,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        CourseKnowledgeRelease.objects.create(
            course_id="course_1",
            version_number=2,
            status="active",
            job=second_job,
            content_checksum="d" * 64,
        )


def test_release_concept_source_identity_is_unique() -> None:
    user = _user()
    _, version = _source_version(user)
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=user,
        change_set_checksum="a" * 64,
    )
    release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        version_number=1,
        status="building",
        job=job,
        content_checksum="c" * 64,
    )
    concept = ReleaseConcept.objects.create(
        release=release,
        concept_id="concept_1",
        name="拥塞控制",
        description="避免网络过载。",
        aliases=[],
    )
    values = {
        "concept": concept,
        "source_version": version,
        "chunk_id": "chunk_1",
        "locator": "page:1;block:1",
        "chunk_text": "拥塞控制用于避免网络过载。",
        "span_start": 0,
        "span_end": 4,
        "relation_type": "definition",
    }
    ReleaseConceptSource.objects.create(**values)

    with pytest.raises(IntegrityError), transaction.atomic():
        ReleaseConceptSource.objects.create(**values)


def test_ingestion_job_rejects_invalid_state_transition() -> None:
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=_user(),
        change_set_checksum="a" * 64,
    )

    job.transition_to("running")
    job.transition_to("succeeded")
    with pytest.raises(ValidationError, match="transition"):
        job.transition_to("running")
