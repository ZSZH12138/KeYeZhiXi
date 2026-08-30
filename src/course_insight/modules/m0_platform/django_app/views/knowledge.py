"""Simplified teacher file staging, confirmation and progress views."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from course_insight.modules.m0_platform.django_app.authz import (
    authorize_scope,
)
from course_insight.modules.m0_platform.django_app.file_validation import (
    FileValidationError,
    ValidatedUpload,
    validate_uploaded_file,
)
from course_insight.modules.m0_platform.django_app.forms.knowledge_files import (
    KnowledgeConfirmForm,
    KnowledgeUploadForm,
    QuestionTextEditForm,
)
from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    CourseSource,
    CourseSourceVersion,
    KnowledgeChangeOperation,
    KnowledgeIngestionJob,
)
from course_insight.modules.m0_platform.django_app.workspace_labels import (
    scope_display,
)


@login_required
@require_GET
def page(
    request: HttpRequest,
    course_id: str,
    class_id: str | None = None,
) -> HttpResponse:
    class_id = _authorized_class(request, course_id, class_id)
    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
        status=CourseClassWorkspace.Status.ACTIVE,
    ).first()
    sources = list(
        CourseSource.objects.filter(
            course_id=course_id,
            class_id=class_id,
            status__in=(CourseSource.Status.STAGED, CourseSource.Status.ACTIVE),
        )
        .prefetch_related("versions")
        .order_by("source_type", "display_name", "created_at")
    )
    rows = [
        {
            "source": source,
            "latest": max(
                source.versions.all(),
                key=lambda version: version.version_number,
                default=None,
            ),
        }
        for source in sources
    ]
    job = _requested_job(request, course_id, class_id)
    return render(
        request,
        "course_insight/teacher/knowledge.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "scope": scope_display(
                course_id=course_id,
                class_id=class_id,
                course_display_name=(
                    "" if workspace is None else workspace.course_display_name
                ),
                class_display_name=(
                    "" if workspace is None else workspace.class_display_name
                ),
            ),
            "rows": rows,
            "upload_form": KnowledgeUploadForm(),
            "confirm_form": KnowledgeConfirmForm(),
            "job": job,
            "job_url": (
                None
                if job is None
                else reverse(
                    "teacher-knowledge-job",
                    kwargs={
                        "course_id": course_id,
                        "class_id": class_id,
                        "job_id": job.pk,
                    },
                )
            ),
        },
    )


@login_required
@require_POST
def upload(
    request: HttpRequest,
    course_id: str,
    class_id: str | None = None,
) -> HttpResponse:
    class_id = _authorized_class(request, course_id, class_id)
    form = KnowledgeUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "请选择文件及其用途。")
        return _redirect_to_page(course_id, class_id)

    successful = 0
    for uploaded in form.cleaned_data["files"]:
        try:
            validated = validate_uploaded_file(
                uploaded,
                source_type=str(form.cleaned_data["source_type"]),
            )
            _stage_new_source(
                course_id=course_id,
                class_id=class_id,
                source_type=str(form.cleaned_data["source_type"]),
                validated=validated,
                user=request.user,
            )
            successful += 1
        except FileValidationError as error:
            messages.error(request, f"{uploaded.name}：{error}")
    if successful:
        messages.success(request, f"已加入 {successful} 个文件，点击“确认处理”后开始解析。")
    return _redirect_to_page(course_id, class_id)


@login_required
@require_POST
def stage_delete(
    request: HttpRequest,
    course_id: str,
    class_id: str | None = None,
) -> HttpResponse:
    class_id = _authorized_class(request, course_id, class_id)
    raw_ids = request.POST.getlist("source_ids")
    try:
        source_ids = [uuid.UUID(value) for value in raw_ids]
    except (TypeError, ValueError):
        messages.error(request, "删除列表无效。")
        return _redirect_to_page(course_id, class_id)
    updated = CourseSource.objects.filter(
        course_id=course_id,
        class_id=class_id,
        source_id__in=source_ids,
        status__in=(CourseSource.Status.STAGED, CourseSource.Status.ACTIVE),
    ).update(status=CourseSource.Status.PENDING_DELETE)
    if updated:
        messages.success(request, f"已将 {updated} 个文件加入待删除列表；确认处理后生效。")
    return _redirect_to_page(course_id, class_id)


@login_required
@require_http_methods(["GET", "POST"])
def edit_question(
    request: HttpRequest,
    course_id: str,
    source_id: uuid.UUID,
    class_id: str | None = None,
) -> HttpResponse:
    class_id = _authorized_class(request, course_id, class_id)
    try:
        source = CourseSource.objects.get(
            pk=source_id,
            course_id=course_id,
            class_id=class_id,
            source_type=CourseSource.SourceType.QUESTION,
            status__in=(CourseSource.Status.STAGED, CourseSource.Status.ACTIVE),
        )
        latest = source.versions.order_by("-version_number").first()
    except CourseSource.DoesNotExist:
        raise Http404 from None
    if latest is None:
        raise Http404

    if request.method == "POST":
        form = QuestionTextEditForm(request.POST)
        if form.is_valid():
            uploaded = SimpleUploadedFile(
                source.display_name,
                str(form.cleaned_data["text"]).encode("utf-8"),
            )
            try:
                validated = validate_uploaded_file(uploaded, source_type="question")
                _stage_question_version(source, validated)
            except FileValidationError as error:
                form.add_error("text", str(error))
            else:
                messages.success(request, "题目文件的新版本已保存，确认处理后重新标注知识点。")
                return _redirect_to_page(course_id, class_id)
    else:
        form = QuestionTextEditForm(initial={"text": _read_text(latest.storage_key)})
    return render(
        request,
        "course_insight/teacher/question_edit.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "source": source,
            "form": form,
        },
    )


@login_required
@require_POST
def confirm(
    request: HttpRequest,
    course_id: str,
    class_id: str | None = None,
) -> HttpResponse:
    class_id = _authorized_class(request, course_id, class_id)
    form = KnowledgeConfirmForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请勾选确认后再开始处理。")
        return _redirect_to_page(course_id, class_id)
    sources = list(
        CourseSource.objects.filter(
            course_id=course_id,
            class_id=class_id,
            status__in=(
                CourseSource.Status.STAGED,
                CourseSource.Status.PENDING_DELETE,
            ),
        ).order_by("source_id")
    )
    if not sources:
        messages.info(request, "当前没有待处理的文件变化。")
        return _redirect_to_page(course_id, class_id)

    operations = [_operation_payload(source) for source in sources]
    serialized = json.dumps(
        {
            "course_id": course_id,
            "class_id": class_id,
            "operations": operations,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    with transaction.atomic():
        job = KnowledgeIngestionJob.objects.create(
            change_set_checksum=checksum,
            course_id=course_id,
            class_id=class_id,
            requested_by=request.user,
        )
        KnowledgeChangeOperation.objects.bulk_create(
            [
                KnowledgeChangeOperation(
                    job=job,
                    sequence=index,
                    operation=payload["operation"],
                    source_id=payload["source_id"],
                    payload=payload,
                )
                for index, payload in enumerate(operations, start=1)
            ]
        )
    messages.success(request, "处理任务已进入队列，页面会自动更新进度。")
    return redirect(
        f"{reverse('teacher-course-knowledge', kwargs={'course_id': course_id, 'class_id': class_id})}?job={job.pk}"
    )


@login_required
@require_GET
def job_status(
    request: HttpRequest,
    course_id: str,
    job_id: uuid.UUID,
    class_id: str | None = None,
) -> JsonResponse:
    class_id = _authorized_class(request, course_id, class_id)
    try:
        job = KnowledgeIngestionJob.objects.get(
            pk=job_id,
            course_id=course_id,
            class_id=class_id,
        )
    except KnowledgeIngestionJob.DoesNotExist:
        raise Http404 from None
    checkpoint = job.checkpoint if isinstance(job.checkpoint, dict) else {}
    waiting_for_worker = (
        job.status == KnowledgeIngestionJob.Status.RUNNING
        and (job.lease_until is None or job.lease_until <= timezone.now())
    )
    detail_keys = (
        "completed_units",
        "total_units",
        "unit_kind",
        "current_batch",
        "total_batches",
        "completed_characters",
        "total_characters",
        "retry_reason",
        "retry_depth",
        "retry_attempt",
        "retry_limit",
        "validation_code",
        "failed_source_errors",
    )
    detail = {
        key: checkpoint[key]
        for key in detail_keys
        if key in checkpoint
    }
    return JsonResponse(
        {
            "job_id": str(job.pk),
            "status": job.status,
            "progress": job.progress,
            "stage": checkpoint.get("stage"),
            "worker_state": (
                "waiting_for_worker"
                if waiting_for_worker
                else "active"
                if job.status == KnowledgeIngestionJob.Status.RUNNING
                else job.status
            ),
            "status_message": (
                "后台处理进程已停止，等待新的处理进程接管"
                if waiting_for_worker
                else "正在处理"
                if job.status == KnowledgeIngestionJob.Status.RUNNING
                else job.get_status_display()
            ),
            "detail": detail,
            "detail_message": _retry_detail_message(detail),
            "activity_at": job.updated_at.isoformat(),
            "error_code": job.error_code,
        }
    )


def _storage_root() -> Path:
    configured = getattr(settings, "COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR", None)
    root = Path(configured) if configured is not None else Path(settings.COURSE_INSIGHT_RUNTIME_DIR) / "knowledge_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _store(payload: bytes) -> tuple[str, Path]:
    storage_key = f"{uuid.uuid4().hex}/{uuid.uuid4()}"
    root = _storage_root()
    target = (root / storage_key).resolve()
    if root not in target.parents:
        raise RuntimeError("opaque storage target escaped its root")
    target.parent.mkdir(parents=True, exist_ok=False)
    with target.open("xb") as stream:
        stream.write(payload)
    return storage_key, target


def _stage_new_source(
    *,
    course_id: str,
    class_id: str,
    source_type: str,
    validated: ValidatedUpload,
    user,
) -> None:
    storage_key, target = _store(validated.payload)
    try:
        with transaction.atomic():
            source = CourseSource.objects.create(
                course_id=course_id,
                class_id=class_id,
                display_name=validated.display_name,
                source_type=source_type,
                status=CourseSource.Status.STAGED,
                created_by=user,
            )
            CourseSourceVersion.objects.create(
                source=source,
                version_number=1,
                storage_key=storage_key,
                sha256=validated.sha256,
                media_type=validated.media_type,
                size_bytes=len(validated.payload),
                status=CourseSourceVersion.Status.STAGED,
            )
    except Exception:
        target.unlink(missing_ok=True)
        target.parent.rmdir()
        raise


def _stage_question_version(source: CourseSource, validated: ValidatedUpload) -> None:
    storage_key, target = _store(validated.payload)
    try:
        with transaction.atomic():
            locked = CourseSource.objects.select_for_update().get(pk=source.pk)
            latest = locked.versions.order_by("-version_number").first()
            next_number = 1 if latest is None else latest.version_number + 1
            CourseSourceVersion.objects.create(
                source=locked,
                version_number=next_number,
                storage_key=storage_key,
                sha256=validated.sha256,
                media_type=validated.media_type,
                size_bytes=len(validated.payload),
                status=CourseSourceVersion.Status.STAGED,
            )
            locked.status = CourseSource.Status.STAGED
            locked.save(update_fields=("status", "updated_at"))
    except Exception:
        target.unlink(missing_ok=True)
        target.parent.rmdir()
        raise


def _read_text(storage_key: str) -> str:
    root = _storage_root()
    target = (root / storage_key).resolve()
    if root not in target.parents or not target.is_file():
        raise Http404
    return target.read_text(encoding="utf-8-sig")


def _operation_payload(source: CourseSource) -> dict[str, str | int | None]:
    latest = source.versions.order_by("-version_number").first()
    if source.status == CourseSource.Status.PENDING_DELETE:
        operation = KnowledgeChangeOperation.Operation.DELETE
    elif source.source_type == CourseSource.SourceType.QUESTION and latest and latest.version_number > 1:
        operation = KnowledgeChangeOperation.Operation.EDIT_QUESTION
    else:
        operation = KnowledgeChangeOperation.Operation.UPSERT
    return {
        "operation": operation,
        "source_id": str(source.pk),
        "source_type": source.source_type,
        "version_id": None if latest is None else str(latest.pk),
        "version_number": None if latest is None else latest.version_number,
    }


def _requested_job(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> KnowledgeIngestionJob | None:
    raw = request.GET.get("job")
    if not raw:
        return None
    try:
        identifier = uuid.UUID(raw)
    except ValueError:
        return None
    return KnowledgeIngestionJob.objects.filter(
        pk=identifier,
        course_id=course_id,
        class_id=class_id,
    ).first()


def _authorized_class(
    request: HttpRequest,
    course_id: str,
    class_id: str | None,
) -> str:
    """Resolve legacy course-only URLs only when one class is unambiguous."""

    selected = class_id
    if selected is None:
        class_ids = list(
            request.user.actor_grants.filter(
                is_active=True,
                revoked_at__isnull=True,
                course_id=course_id,
                class_id__isnull=False,
            )
            .order_by("class_id")
            .values_list("class_id", flat=True)
            .distinct()
        )
        if len(class_ids) != 1:
            raise PermissionDenied
        selected = str(class_ids[0])
    authorize_scope(
        request.user,
        "manage_course_knowledge",
        course_id=course_id,
        class_id=selected,
    )
    return selected


def _redirect_to_page(course_id: str, class_id: str):
    return redirect(
        "teacher-course-knowledge",
        course_id=course_id,
        class_id=class_id,
    )


def _retry_detail_message(detail: dict[str, object]) -> str | None:
    source_errors = detail.get("failed_source_errors")
    if isinstance(source_errors, dict) and source_errors:
        codes = {
            code
            for code in source_errors.values()
            if isinstance(code, str)
        }
        if codes == {"LEGACY_PPT_CONVERSION_UNAVAILABLE"}:
            return (
                f"有 {len(source_errors)} 个旧版 PPT 未能处理："
                "当前服务器无法调用 Microsoft PowerPoint。"
            )
        if codes <= {
            "LEGACY_PPT_CONVERSION_UNAVAILABLE",
            "LEGACY_PPT_CONVERSION_FAILED",
        }:
            return (
                f"有 {len(source_errors)} 个旧版 PPT 未能完成转换，"
                "请检查文件是否损坏后重试。"
            )
        return f"有 {len(source_errors)} 个文件未能处理，请检查文件状态。"
    retry_reason = detail.get("retry_reason")
    if retry_reason == "KNOWLEDGE_EXTRACTION_OUTPUT_INVALID":
        values = tuple(
            detail.get(key)
            for key in (
                "current_batch",
                "total_batches",
                "retry_attempt",
                "retry_limit",
                "completed_characters",
                "total_characters",
            )
        )
        if any(type(value) is not int or value < 0 for value in values):
            return None
        current_batch, total_batches, attempt, limit, completed, total = values
        return (
            f"第 {current_batch}/{total_batches} 批输出未通过格式或溯源校验，"
            f"正在进行第 {attempt}/{limit} 次重试；"
            f"已完成 {completed}/{total} 字"
        )
    if retry_reason != "DEEPSEEK_INCOMPLETE_RESPONSE":
        return None
    values = tuple(
        detail.get(key)
        for key in (
            "current_batch",
            "total_batches",
            "retry_depth",
            "completed_characters",
            "total_characters",
        )
    )
    if any(type(value) is not int or value < 0 for value in values):
        return None
    current_batch, total_batches, retry_depth, completed, total = values
    return (
        f"第 {current_batch}/{total_batches} 批响应不完整，"
        f"正在缩小文本后重试（第 {retry_depth} 层）；"
        f"已完成 {completed}/{total} 字"
    )
