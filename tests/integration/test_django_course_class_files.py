from __future__ import annotations

import hashlib
import uuid

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeIngestionJob,
    ReleaseConcept,
    ReleaseConceptSource,
    User,
)


pytestmark = pytest.mark.django_db


def _user(role: str, *, class_id: str = "class_1") -> User:
    actor_id = f"pseudonym_{role}_files_{class_id}"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    permission_name = (
        "manage_course_knowledge" if role == "teacher" else "view_course_files"
    )
    group, _ = Group.objects.get_or_create(name=f"files-{role}-{class_id}")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename=permission_name,
        )
    )
    user.groups.add(group)
    ActorGrant.objects.create(
        user=user,
        role=role,
        course_id="course_1",
        class_id=class_id,
        source_checksum="a" * 64,
    )
    return user


def _published_knowledge(root, teacher: User, *, class_id: str, name: str):
    payload = f"{class_id}:{name}".encode()
    storage_key = f"{uuid.uuid4().hex}/{uuid.uuid4()}"
    target = root / storage_key
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    source = CourseSource.objects.create(
        course_id="course_1",
        class_id=class_id,
        display_name=name,
        source_type="knowledge",
        status="active",
        created_by=teacher,
    )
    version = CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=storage_key,
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="text/plain",
        size_bytes=len(payload),
        status="active",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        class_id=class_id,
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(class_id.encode()).hexdigest(),
        status="succeeded",
        progress=100,
    )
    release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        class_id=class_id,
        version_number=1,
        status="active",
        job=job,
        content_checksum=hashlib.sha256(name.encode()).hexdigest(),
    )
    concept = ReleaseConcept.objects.create(
        release=release,
        concept_id=f"concept-{class_id}",
        name=f"知识点 {class_id}",
        description="课程原文",
        aliases=[],
    )
    ReleaseConceptSource.objects.create(
        concept=concept,
        source_version=version,
        chunk_id=f"chunk-{class_id}",
        locator="paragraph:1",
        chunk_text=payload.decode(),
        span_start=0,
        span_end=1,
        relation_type="definition",
    )
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id=class_id,
        active_release=release,
        content_revision=1,
    )
    return source, version, workspace, payload


@override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=None)
def test_student_file_routes_require_exact_scope(client: Client) -> None:
    client.force_login(_user("student", class_id="class_2"))

    response = client.get(
        reverse(
            "student-course-files",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        )
    )

    assert response.status_code == 403


def test_student_sees_and_downloads_only_active_knowledge_files(
    client: Client, tmp_path
) -> None:
    teacher = _user("teacher")
    student = _user("student")
    source, _, _, payload = _published_knowledge(
        tmp_path, teacher, class_id="class_1", name="chapter.txt"
    )
    question = CourseSource.objects.create(
        course_id="course_1",
        class_id="class_1",
        display_name="questions.txt",
        source_type="question",
        status="active",
        created_by=teacher,
    )
    client.force_login(student)

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        page = client.get(
            reverse(
                "student-course-files",
                kwargs={"course_id": "course_1", "class_id": "class_1"},
            )
        )
        download = client.get(
            reverse(
                "student-course-file-download",
                kwargs={
                    "course_id": "course_1",
                    "class_id": "class_1",
                    "source_id": source.pk,
                },
            )
        )
        denied = client.get(
            reverse(
                "student-course-file-download",
                kwargs={
                    "course_id": "course_1",
                    "class_id": "class_1",
                    "source_id": question.pk,
                },
            )
        )

    assert "chapter.txt" in page.content.decode()
    assert "questions.txt" not in page.content.decode()
    assert b"".join(download.streaming_content) == payload
    assert denied.status_code == 404


def test_deleted_knowledge_file_disappears_immediately(client: Client, tmp_path) -> None:
    teacher = _user("teacher")
    student = _user("student")
    source, _, _, _ = _published_knowledge(
        tmp_path, teacher, class_id="class_1", name="deleted.txt"
    )
    client.force_login(student)
    source.status = "deleted"
    source.save(update_fields=("status", "updated_at"))

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        page = client.get(
            reverse(
                "student-course-files",
                kwargs={"course_id": "course_1", "class_id": "class_1"},
            )
        )
        download = client.get(
            reverse(
                "student-course-file-download",
                kwargs={
                    "course_id": "course_1",
                    "class_id": "class_1",
                    "source_id": source.pk,
                },
            )
        )

    assert "deleted.txt" not in page.content.decode()
    assert download.status_code == 404


def test_teacher_upload_mutates_only_the_selected_class(client: Client, tmp_path) -> None:
    teacher = _user("teacher", class_id="class_1")
    ActorGrant.objects.create(
        user=teacher,
        role="teacher",
        course_id="course_1",
        class_id="class_2",
        source_checksum="b" * 64,
    )
    client.force_login(teacher)

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(
            reverse(
                "teacher-knowledge-upload",
                kwargs={"course_id": "course_1", "class_id": "class_2"},
            ),
            {
                "source_type": "knowledge",
                "files": SimpleUploadedFile("class-two.txt", b"class two"),
            },
        )

    assert response.status_code == 302
    assert CourseSource.objects.filter(
        course_id="course_1",
        class_id="class_2",
        display_name="class-two.txt",
    ).exists()
    assert not CourseSource.objects.filter(
        course_id="course_1",
        class_id="class_1",
        display_name="class-two.txt",
    ).exists()


def test_teacher_home_links_each_exact_class_workspace(client: Client) -> None:
    teacher = _user("teacher", class_id="class_1")
    teacher.groups.get().permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="view_class_analytics",
        )
    )
    ActorGrant.objects.create(
        user=teacher,
        role="teacher",
        course_id="course_1",
        class_id="class_2",
        source_checksum="b" * 64,
    )
    client.force_login(teacher)

    response = client.get(reverse("teacher-home"))

    body = response.content.decode()
    assert reverse(
        "teacher-course-knowledge",
        kwargs={"course_id": "course_1", "class_id": "class_1"},
    ) in body
    assert reverse(
        "teacher-course-knowledge",
        kwargs={"course_id": "course_1", "class_id": "class_2"},
    ) in body
