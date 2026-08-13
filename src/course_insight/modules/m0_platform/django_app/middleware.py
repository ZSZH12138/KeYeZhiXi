"""Safe request limits, correlation context, and domain error handling."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app.error_mapping import (
    render_domain_error,
    render_safe_error,
)


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


class M0RequestMiddleware:
    """Reject oversized bodies before parsing and bind safe log identity."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponse],
    ) -> None:
        self._get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if self._too_large(request):
            return render_safe_error(request, status=413)
        request_id = request.headers.get("X-Request-ID", "")
        if not _REQUEST_ID.fullmatch(request_id):
            request_id = f"web-{uuid.uuid4().hex}"
        setattr(request, "course_insight_request_id", request_id)
        actor_id = self._safe_actor_id(request)
        try:
            with bind_log_context(
                request_id=request_id,
                actor_id=actor_id,
            ):
                response = self._get_response(request)
        except DomainError as error:
            response = render_domain_error(request, error)
        response.headers["X-Request-ID"] = request_id
        return response

    @staticmethod
    def process_exception(
        request: HttpRequest,
        exception: BaseException,
    ) -> HttpResponse | None:
        if isinstance(exception, DomainError):
            return render_domain_error(request, exception)
        return None

    @staticmethod
    def _too_large(request: HttpRequest) -> bool:
        raw_length = request.META.get("CONTENT_LENGTH")
        if raw_length in {None, ""}:
            return False
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return True
        return length < 0 or length > settings.DATA_UPLOAD_MAX_MEMORY_SIZE

    @staticmethod
    def _safe_actor_id(request: HttpRequest) -> str | None:
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return None
        actor_id = getattr(user, "actor_id", None)
        return (
            actor_id
            if isinstance(actor_id, str)
            and _REQUEST_ID.fullmatch(actor_id)
            else None
        )
