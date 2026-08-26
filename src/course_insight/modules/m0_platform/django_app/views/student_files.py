"""Student-visible knowledge files from one current course/class release."""

from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from course_insight.modules.m0_platform.django_app.authz import authorize_scope
from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    CourseSource,
    CourseSourceVersion,
)


@login_required
@require_GET
def page(request: HttpRequest, course_id: str, class_id: str) -> HttpResponse:
    """List active knowledge files referenced by the current release."""

    _authorize(request, course_id=course_id, class_id=class_id)
    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
    ).first()
    sources = list(
        CourseSource.objects.filter(
            course_id=course_id,
            class_id=class_id,
            source_type=CourseSource.SourceType.KNOWLEDGE,
            status=CourseSource.Status.ACTIVE,
            versions__status=CourseSourceVersion.Status.ACTIVE,
            versions__concept_references__concept__release_id=(
                None if workspace is None else workspace.active_release_id
            ),
            versions__concept_references__concept__release__status="active",
        )
        .distinct()
        .order_by("display_name", "source_id")
    )
    return render(
        request,
        "course_insight/student/files.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "sources": sources,
            "content_revision": (
                0 if workspace is None else workspace.content_revision
            ),
        },
    )


@login_required
@require_GET
def download(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    source_id: uuid.UUID,
) -> FileResponse:
    """Download one active knowledge file without exposing its storage key."""

    _authorize(request, course_id=course_id, class_id=class_id)
    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
        active_release__status="active",
    ).first()
    if workspace is None:
        raise Http404
    try:
        source = CourseSource.objects.get(
            pk=source_id,
            course_id=course_id,
            class_id=class_id,
            source_type=CourseSource.SourceType.KNOWLEDGE,
            status=CourseSource.Status.ACTIVE,
        )
    except CourseSource.DoesNotExist:
        raise Http404 from None
    version = (
        source.versions.filter(
            status=CourseSourceVersion.Status.ACTIVE,
            concept_references__concept__release_id=workspace.active_release_id,
            concept_references__concept__release__status="active",
        )
        .distinct()
        .order_by("-version_number")
        .first()
    )
    if version is None:
        raise Http404
    root = Path(settings.COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR).resolve()
    target = (root / version.storage_key).resolve()
    if root not in target.parents or not target.is_file():
        raise Http404
    return FileResponse(
        target.open("rb"),
        as_attachment=True,
        filename=source.display_name,
        content_type=version.media_type,
    )


def _authorize(request: HttpRequest, *, course_id: str, class_id: str) -> None:
    authorize_scope(
        request.user,
        "view_course_files",
        course_id=course_id,
        class_id=class_id,
        learner_id=request.user.actor_id,
    )
