from __future__ import annotations

from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile, TemporaryUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory

from course_insight.modules.m0_platform.django_app.middleware import (
    M0RequestMiddleware,
)


def test_five_gib_request_budget_keeps_large_uploads_disk_backed() -> None:
    request = RequestFactory().post(
        "/teacher/courses/course_1/classes/class_1/knowledge/uploads/",
        CONTENT_LENGTH=str(5 * 1024**3 + 1024**2),
    )
    request.user = SimpleNamespace(
        is_authenticated=True,
        has_perm=lambda permission: permission
        == "m0_platform_web.manage_course_knowledge",
    )

    response = M0RequestMiddleware(
        lambda current: HttpResponse(status=204)
    )(request)

    assert response.status_code == 204

    multipart_request = RequestFactory().post(
        "/upload",
        {
            "files": SimpleUploadedFile(
                "large-enough-for-disk.pptx",
                b"x" * (9 * 1024**2),
            )
        },
    )
    assert isinstance(
        multipart_request.FILES["files"],
        TemporaryUploadedFile,
    )


def test_large_request_budget_is_not_available_to_other_requests() -> None:
    middleware = M0RequestMiddleware(
        lambda current: HttpResponse(status=204)
    )
    anonymous_upload = RequestFactory().post(
        "/teacher/courses/course_1/classes/class_1/knowledge/uploads/",
        CONTENT_LENGTH=str(5 * 1024**3),
    )
    anonymous_upload.user = SimpleNamespace(
        is_authenticated=False,
        has_perm=lambda permission: False,
    )
    ordinary_request = RequestFactory().post(
        "/accounts/login/",
        CONTENT_LENGTH=str(2 * 1024**2),
    )
    ordinary_request.user = SimpleNamespace(
        is_authenticated=True,
        has_perm=lambda permission: True,
    )

    assert middleware(anonymous_upload).status_code == 413
    assert middleware(ordinary_request).status_code == 413
