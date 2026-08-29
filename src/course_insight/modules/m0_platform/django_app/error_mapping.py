"""Central, non-disclosing DomainError to HTTP mapping."""

from __future__ import annotations

from collections.abc import Mapping

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from course_insight.contracts.errors import DomainError


_EXACT_STATUS: Mapping[str, int] = {
    "ASSESSMENT_SCOPE_MISMATCH": 403,
    "ASSESSMENT_NOT_FOUND": 404,
    "AUDIT_NOT_FOUND": 404,
    "RESULT_NOT_FOUND": 404,
    "WORKFLOW_RESULT_UNAVAILABLE": 404,
    "ASSESSMENT_ALREADY_SUBMITTED": 409,
    "WORKFLOW_BUSY": 409,
    "WORKFLOW_REQUEST_CONFLICT": 409,
    "AUDIT_VERSION_CONFLICT": 409,
    "CLASS_AGGREGATION_INVALID": 503,
    "CLASS_ROSTER_INVALID": 409,
    "CLASS_ROSTER_UNAVAILABLE": 503,
    "DATABASE_UNAVAILABLE": 503,
    "EVIDENCE_REQUIRED": 503,
    "RUNTIME_CONTEXT_UNAVAILABLE": 503,
    "RUNTIME_MANIFEST_INVALID": 503,
    "RUNTIME_SNAPSHOT_INVALID": 503,
    "LOG_SINK_UNAVAILABLE": 503,
}
_SAFE_MESSAGES: Mapping[int, str] = {
    400: "请求内容无效，请检查后重试。",
    403: "你无权访问该资源。",
    404: "请求的资源不存在。",
    409: "资源已发生变化，请刷新后重试。",
    413: "请求内容过大。",
    500: "服务暂时无法处理该请求。",
    503: "服务尚未就绪，请稍后重试。",
}


def domain_error_status(error: DomainError) -> int:
    """Map only safe error-code categories; never inspect details."""

    exact = _EXACT_STATUS.get(error.code)
    if exact is not None:
        return exact
    code = error.code
    if "SCOPE" in code or "PERMISSION" in code or "FORBIDDEN" in code:
        return 403
    if "NOT_FOUND" in code:
        return 404
    if "VERSION" in code or "CONFLICT" in code or "BUSY" in code:
        return 409
    if (
        "UNAVAILABLE" in code
        or "CONNECTION" in code
        or "MIGRATION" in code
    ):
        return 503
    return 400


def render_domain_error(
    request: HttpRequest,
    error: DomainError,
) -> HttpResponse:
    status = domain_error_status(error)
    return render_safe_error(request, status=status)


def render_safe_error(
    request: HttpRequest,
    *,
    status: int,
) -> HttpResponse:
    """Render a fixed message with no exception, path, SQL, or secret."""

    safe_status = status if status in _SAFE_MESSAGES else 500
    return render(
        request,
        "course_insight/error.html",
        {
            "status_code": safe_status,
            "safe_message": _SAFE_MESSAGES[safe_status],
        },
        status=safe_status,
    )


def bad_request(
    request: HttpRequest,
    exception: BaseException | None = None,
) -> HttpResponse:
    del exception
    return render_safe_error(request, status=400)


def permission_denied(
    request: HttpRequest,
    exception: BaseException | None = None,
) -> HttpResponse:
    del exception
    return render_safe_error(request, status=403)


def page_not_found(
    request: HttpRequest,
    exception: BaseException | None = None,
) -> HttpResponse:
    del exception
    return render_safe_error(request, status=404)


def server_error(request: HttpRequest) -> HttpResponse:
    return render_safe_error(request, status=500)
