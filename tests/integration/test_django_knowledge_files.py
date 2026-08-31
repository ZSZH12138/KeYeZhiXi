from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    CourseSource,
    CourseSourceVersion,
    KnowledgeIngestionJob,
    User,
)


pytestmark = pytest.mark.django_db

_OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def _teacher(course_id: str = "course_1") -> User:
    actor_id = f"pseudonym_teacher_{course_id}"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    group, _ = Group.objects.get_or_create(name=f"knowledge-{course_id}")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="manage_course_knowledge",
        )
    )
    user.groups.add(group)
    ActorGrant.objects.create(
        user=user,
        role="teacher",
        course_id=course_id,
        class_id="class_1",
        source_checksum="a" * 64,
    )
    return user


@override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=None)
def test_anonymous_redirect_and_cross_course_access_is_denied(client: Client) -> None:
    url = reverse("teacher-course-knowledge", kwargs={"course_id": "course_1"})
    assert client.get(url).status_code == 302

    client.force_login(_teacher("course_2"))
    assert client.get(url).status_code == 403


def test_state_changing_upload_requires_csrf(tmp_path) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(_teacher())
    url = reverse("teacher-knowledge-upload", kwargs={"course_id": "course_1"})

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(
            url,
            {
                "source_type": "knowledge",
                "files": SimpleUploadedFile("chapter.md", b"# Chapter"),
            },
        )

    assert response.status_code == 403


def test_multiple_mixed_knowledge_files_can_be_staged(tmp_path) -> None:
    client = Client()
    client.force_login(_teacher())
    url = reverse("teacher-knowledge-upload", kwargs={"course_id": "course_1"})
    files = [
        SimpleUploadedFile("chapter.md", "# 第一章".encode()),
        SimpleUploadedFile("notes.txt", "拥塞控制".encode()),
    ]

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(url, {"source_type": "knowledge", "files": files})

    assert response.status_code == 302
    assert CourseSource.objects.filter(course_id="course_1", status="staged").count() == 2
    assert CourseSourceVersion.objects.count() == 2
    assert len(list(tmp_path.rglob("*.*"))) == 0  # opaque files have no extension
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 2


def test_genuine_legacy_powerpoint_can_be_staged_with_original_name(tmp_path) -> None:
    client = Client()
    client.force_login(_teacher())
    url = reverse("teacher-knowledge-upload", kwargs={"course_id": "course_1"})

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(
            url,
            {
                "source_type": "knowledge",
                "files": SimpleUploadedFile(
                    "计算机网络.ppt",
                    _OLE_COMPOUND_FILE_SIGNATURE + b"legacy presentation payload",
                ),
            },
        )

    assert response.status_code == 302
    source = CourseSource.objects.get()
    version = source.versions.get()
    assert source.display_name == "计算机网络.ppt"
    assert version.media_type == "application/vnd.ms-powerpoint"


def test_upload_larger_than_one_mib_reaches_file_validation(tmp_path) -> None:
    client = Client()
    client.force_login(_teacher())
    url = reverse(
        "teacher-knowledge-upload",
        kwargs={"course_id": "course_1"},
    )
    payload = ("计算机网络课程内容\n" * 80_000).encode("utf-8")
    assert len(payload) > 1024**2

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(
            url,
            {
                "source_type": "knowledge",
                "files": SimpleUploadedFile("large-notes.txt", payload),
            },
        )

    assert response.status_code == 302
    version = CourseSourceVersion.objects.get()
    assert version.size_bytes == len(payload)


def test_invalid_sibling_does_not_rollback_valid_upload(tmp_path) -> None:
    client = Client()
    client.force_login(_teacher())
    url = reverse("teacher-knowledge-upload", kwargs={"course_id": "course_1"})

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(
            url,
            {
                "source_type": "knowledge",
                "files": [
                    SimpleUploadedFile("valid.txt", b"valid course text"),
                    SimpleUploadedFile("false.pptx", b"not pptx"),
                ],
            },
        )

    assert response.status_code == 302
    assert list(CourseSource.objects.values_list("display_name", flat=True)) == ["valid.txt"]


def test_multi_delete_is_staged_then_confirm_creates_job(tmp_path) -> None:
    teacher = _teacher()
    source_ids = []
    for index in range(2):
        source = CourseSource.objects.create(
            course_id="course_1",
            display_name=f"chapter-{index}.txt",
            source_type="knowledge",
            status="active",
            created_by=teacher,
        )
        source_ids.append(str(source.pk))
    client = Client()
    client.force_login(teacher)

    delete_response = client.post(
        reverse("teacher-knowledge-stage-delete", kwargs={"course_id": "course_1"}),
        {"source_ids": source_ids},
    )
    confirm_response = client.post(
        reverse("teacher-knowledge-confirm", kwargs={"course_id": "course_1"}),
        {"confirm": "on"},
    )

    assert delete_response.status_code == 302
    assert CourseSource.objects.filter(status="pending_delete").count() == 2
    assert confirm_response.status_code == 302
    job = KnowledgeIngestionJob.objects.get()
    assert job.operations.filter(operation="delete_source").count() == 2


def test_pending_and_completed_deletions_are_hidden_from_teacher_page() -> None:
    teacher = _teacher()
    for display_name, status in (
        ("active.txt", "active"),
        ("pending.txt", "pending_delete"),
        ("deleted.txt", "deleted"),
    ):
        CourseSource.objects.create(
            course_id="course_1",
            display_name=display_name,
            source_type="knowledge",
            status=status,
            created_by=teacher,
        )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse("teacher-course-knowledge", kwargs={"course_id": "course_1"})
    )

    body = response.content.decode()
    assert "active.txt" in body
    assert "pending.txt" not in body
    assert "deleted.txt" not in body


def test_confirm_does_not_requeue_a_completed_deletion() -> None:
    teacher = _teacher()
    CourseSource.objects.create(
        course_id="course_1",
        display_name="already-deleted.txt",
        source_type="knowledge",
        status="deleted",
        created_by=teacher,
    )
    client = Client()
    client.force_login(teacher)

    response = client.post(
        reverse("teacher-knowledge-confirm", kwargs={"course_id": "course_1"}),
        {"confirm": "on"},
    )

    assert response.status_code == 302
    assert not KnowledgeIngestionJob.objects.exists()


@pytest.mark.parametrize("status", ["pending_delete", "deleted"])
def test_non_active_question_file_cannot_be_opened_for_editing(
    status: str,
    tmp_path,
) -> None:
    teacher = _teacher()
    source = CourseSource.objects.create(
        course_id="course_1",
        display_name="questions.txt",
        source_type="question",
        status=status,
        created_by=teacher,
    )
    storage_key = "b" * 32 + "/00000000-0000-0000-0000-000000000002"
    target = tmp_path / storage_key
    target.parent.mkdir(parents=True)
    payload = "[QUESTION]\nid: q-1\ntype: subjective\nstem: 示例\nanswer: 示例\nrubric: 示例\nexplanation: 示例\n[/QUESTION]".encode()
    target.write_bytes(payload)
    CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=storage_key,
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="text/plain",
        size_bytes=len(payload),
        status="active",
    )
    client = Client()
    client.force_login(teacher)

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.get(
            reverse(
                "teacher-knowledge-question-edit",
                kwargs={"course_id": "course_1", "source_id": source.pk},
            )
        )

    assert response.status_code == 404


def test_failed_change_set_can_be_confirmed_as_a_new_job(tmp_path) -> None:
    teacher = _teacher()
    client = Client()
    client.force_login(teacher)
    upload_url = reverse(
        "teacher-knowledge-upload",
        kwargs={"course_id": "course_1"},
    )
    confirm_url = reverse(
        "teacher-knowledge-confirm",
        kwargs={"course_id": "course_1"},
    )

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        client.post(
            upload_url,
            {
                "source_type": "knowledge",
                "files": [SimpleUploadedFile("retry.txt", b"course content")],
            },
        )
        client.post(confirm_url, {"confirm": "on"})
        first = KnowledgeIngestionJob.objects.get()
        first.status = KnowledgeIngestionJob.Status.FAILED
        first.progress = 100
        first.error_code = "KNOWLEDGE_EXTRACTION_OUTPUT_INVALID"
        first.save(update_fields=("status", "progress", "error_code"))

        client.post(confirm_url, {"confirm": "on"})

    jobs = list(KnowledgeIngestionJob.objects.order_by("created_at"))
    assert len(jobs) == 2
    assert jobs[0].change_set_checksum == jobs[1].change_set_checksum
    assert jobs[1].status == KnowledgeIngestionJob.Status.QUEUED
    assert jobs[1].operations.count() == 1


def test_expired_running_job_reports_waiting_for_worker() -> None:
    teacher = _teacher()
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(b"expired-worker-status").hexdigest(),
        status=KnowledgeIngestionJob.Status.RUNNING,
        progress=25,
        checkpoint={
            "stage": "extracting_concepts",
            "current_batch": 2,
            "total_batches": 4,
            "completed_characters": 1200,
            "total_characters": 3600,
        },
        worker_id="missing-worker",
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-knowledge-job",
            kwargs={"course_id": "course_1", "job_id": job.pk},
        )
    )

    payload = response.json()
    assert payload["worker_state"] == "waiting_for_worker"
    assert payload["status_message"] == "后台处理进程已停止，等待新的处理进程接管"
    assert payload["activity_at"] == job.updated_at.isoformat()
    assert payload["detail"] == {
        "current_batch": 2,
        "total_batches": 4,
        "completed_characters": 1200,
        "total_characters": 3600,
    }


def test_queued_job_reports_missing_ingestion_worker(tmp_path) -> None:
    teacher = _teacher()
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(b"queued-without-worker").hexdigest(),
    )
    client = Client()
    client.force_login(teacher)

    with override_settings(COURSE_INSIGHT_RUNTIME_DIR=tmp_path):
        response = client.get(
            reverse(
                "teacher-knowledge-job",
                kwargs={"course_id": "course_1", "job_id": job.pk},
            )
        )

    payload = response.json()
    assert payload["status"] == KnowledgeIngestionJob.Status.QUEUED
    assert payload["worker_state"] == "waiting_for_worker"
    assert payload["status_message"] == (
        "后台处理进程未启动，任务正在等待处理"
    )


def test_incomplete_response_retry_reports_text_shrink_progress() -> None:
    teacher = _teacher()
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(b"incomplete-retry-status").hexdigest(),
        status=KnowledgeIngestionJob.Status.RUNNING,
        progress=26,
        checkpoint={
            "stage": "extracting_concepts",
            "current_batch": 2,
            "total_batches": 34,
            "completed_characters": 8758,
            "total_characters": 182995,
            "retry_reason": "DEEPSEEK_INCOMPLETE_RESPONSE",
            "retry_depth": 1,
        },
        worker_id="active-worker",
        lease_until=timezone.now() + timedelta(minutes=1),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-knowledge-job",
            kwargs={"course_id": "course_1", "job_id": job.pk},
        )
    )

    payload = response.json()
    assert payload["detail"]["retry_reason"] == "DEEPSEEK_INCOMPLETE_RESPONSE"
    assert payload["detail"]["retry_depth"] == 1
    assert payload["detail_message"] == (
        "第 2/34 批响应不完整，正在缩小文本后重试（第 1 层）；"
        "已完成 8758/182995 字"
    )


def test_output_validation_retry_reports_attempt_without_fake_progress() -> None:
    teacher = _teacher()
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(b"output-retry-status").hexdigest(),
        status=KnowledgeIngestionJob.Status.RUNNING,
        progress=30,
        checkpoint={
            "stage": "extracting_concepts",
            "current_batch": 7,
            "total_batches": 34,
            "completed_characters": 35343,
            "total_characters": 182995,
            "retry_reason": "KNOWLEDGE_EXTRACTION_OUTPUT_INVALID",
            "retry_depth": 0,
            "retry_attempt": 3,
            "retry_limit": 5,
            "validation_code": "evidence_quote_not_found",
        },
        worker_id="active-worker",
        lease_until=timezone.now() + timedelta(minutes=1),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-knowledge-job",
            kwargs={"course_id": "course_1", "job_id": job.pk},
        )
    )

    payload = response.json()
    assert payload["detail"]["retry_attempt"] == 3
    assert payload["detail"]["retry_limit"] == 5
    assert payload["detail"]["validation_code"] == "evidence_quote_not_found"
    assert payload["detail_message"] == (
        "第 7/34 批输出未通过格式或溯源校验，正在进行第 3/5 次重试；"
        "已完成 35343/182995 字"
    )


def test_partial_legacy_ppt_failure_reports_actionable_message() -> None:
    teacher = _teacher()
    source_id = "cbe2f523-4e62-4f07-aea3-25d48e1f5681"
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(b"legacy-ppt-partial").hexdigest(),
        status=KnowledgeIngestionJob.Status.PARTIAL,
        progress=100,
        checkpoint={
            "stage": "published",
            "failed_source_errors": {
                source_id: "LEGACY_PPT_CONVERSION_UNAVAILABLE"
            },
        },
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-knowledge-job",
            kwargs={"course_id": "course_1", "job_id": job.pk},
        )
    )

    payload = response.json()
    assert payload["detail"]["failed_source_errors"] == {
        source_id: "LEGACY_PPT_CONVERSION_UNAVAILABLE"
    }
    assert payload["detail_message"] == (
        "有 1 个旧版 PPT 未能处理：当前服务器无法调用 Microsoft PowerPoint。"
    )


def test_teacher_progress_panel_has_live_activity_and_detail_regions() -> None:
    teacher = _teacher()
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=teacher,
        change_set_checksum=hashlib.sha256(b"live-progress-panel").hexdigest(),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse("teacher-course-knowledge", kwargs={"course_id": "course_1"}),
        {"job": str(job.pk)},
    )

    body = response.content.decode()
    assert 'id="job-progress-bar"' in body
    assert 'id="job-detail"' in body
    assert 'aria-live="polite"' in body


def test_question_text_edit_creates_new_immutable_version(tmp_path) -> None:
    teacher = _teacher()
    source = CourseSource.objects.create(
        course_id="course_1",
        display_name="questions.txt",
        source_type="question",
        status="active",
        created_by=teacher,
    )
    storage_key = "a" * 32 + "/00000000-0000-0000-0000-000000000001"
    target = tmp_path / storage_key
    target.parent.mkdir(parents=True)
    target.write_text("旧题目", encoding="utf-8")
    CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=storage_key,
        sha256="a" * 64,
        media_type="text/plain",
        size_bytes=9,
        status="active",
    )
    client = Client()
    client.force_login(teacher)

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path):
        response = client.post(
            reverse(
                "teacher-knowledge-question-edit",
                kwargs={"course_id": "course_1", "source_id": source.pk},
            ),
            {"text": "[填空题]\n题干：拥塞窗口称为____。\n答案：cwnd"},
        )

    assert response.status_code == 302
    assert source.versions.count() == 2
    assert source.versions.order_by("-version_number").first().status == "staged"
