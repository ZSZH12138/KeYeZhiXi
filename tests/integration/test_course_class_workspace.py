from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    KnowledgeIngestionJob,
    User,
)


pytestmark = pytest.mark.django_db


def _teacher() -> User:
    return User.objects.create_user(
        username="pseudonym_teacher_workspace",
        actor_id="pseudonym_teacher_workspace",
    )


def _job(user: User, class_id: str, checksum: str) -> KnowledgeIngestionJob:
    return KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        class_id=class_id,
        requested_by=user,
        change_set_checksum=checksum * 64,
    )


def test_one_course_has_independent_class_workspaces_and_active_releases() -> None:
    teacher = _teacher()
    first_workspace = CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_1",
    )
    second_workspace = CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_2",
    )
    first_release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        class_id="class_1",
        version_number=1,
        status="active",
        job=_job(teacher, "class_1", "a"),
        content_checksum="c" * 64,
    )
    second_release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        class_id="class_2",
        version_number=1,
        status="active",
        job=_job(teacher, "class_2", "b"),
        content_checksum="d" * 64,
    )
    first_workspace.active_release = first_release
    first_workspace.content_revision = 1
    first_workspace.save(update_fields=("active_release", "content_revision"))
    second_workspace.active_release = second_release
    second_workspace.content_revision = 1
    second_workspace.save(update_fields=("active_release", "content_revision"))

    assert first_workspace.active_release_id == first_release.pk
    assert second_workspace.active_release_id == second_release.pk
    assert CourseKnowledgeRelease.objects.filter(
        course_id="course_1", status="active"
    ).count() == 2


def test_only_one_active_release_exists_per_exact_course_class_scope() -> None:
    teacher = _teacher()
    CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        class_id="class_1",
        version_number=1,
        status="active",
        job=_job(teacher, "class_1", "a"),
        content_checksum="c" * 64,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        CourseKnowledgeRelease.objects.create(
            course_id="course_1",
            class_id="class_1",
            version_number=2,
            status="active",
            job=_job(teacher, "class_1", "b"),
            content_checksum="d" * 64,
        )


def test_release_version_numbers_restart_for_another_class() -> None:
    teacher = _teacher()
    for class_id, checksum in (("class_1", "a"), ("class_2", "b")):
        CourseKnowledgeRelease.objects.create(
            course_id="course_1",
            class_id=class_id,
            version_number=1,
            status="active",
            job=_job(teacher, class_id, checksum),
            content_checksum=checksum * 64,
        )

    assert list(
        CourseKnowledgeRelease.objects.order_by("class_id").values_list(
            "class_id", "version_number"
        )
    ) == [("class_1", 1), ("class_2", 1)]
