"""Django-hashed login/logout with shared, hashed failure throttling."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from course_insight.modules.m0_platform.django_app.authz import (
    is_login_allowed,
    register_login_failure,
    resolve_account_type,
    reset_login_failures,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    AuthenticatedSession,
    account_name_digest,
)
from course_insight.modules.m0_platform.django_app.forms.auth import (
    AccountAuthenticationForm,
)


@never_cache
@require_http_methods(["GET", "POST"])
def login(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return _home_redirect(request)
    next_url = _safe_next(request)
    if request.method == "GET":
        return render(
            request,
            "course_insight/login.html",
            {"form": AccountAuthenticationForm(request), "next": next_url},
        )

    form = AccountAuthenticationForm(request, data=request.POST)
    account_name = request.POST.get("username", "")
    actor_hint = _login_rate_limit_hint(account_name)
    client_ip = request.META.get("REMOTE_ADDR") or "0.0.0.0"
    if not _single_login_values(request):
        actor_hint = actor_hint or "invalid-login"
        allowed = False
    else:
        allowed = is_login_allowed(
            actor_hint=actor_hint,
            client_ip=client_ip,
            secret=settings.SECRET_KEY,
        )
    if not allowed:
        form.add_error(None, "登录暂时受限，请稍后重试。")
        return render(
            request,
            "course_insight/login.html",
            {"form": form, "next": next_url},
            status=429,
        )

    if form.is_valid():
        user = form.get_user()
        reset_login_failures(
            actor_hint=actor_hint,
            client_ip=client_ip,
            secret=settings.SECRET_KEY,
            clear_shared_ip=False,
        )
        django_login(request, user)
        request.session.save()
        if request.session.session_key:
            AuthenticatedSession.objects.update_or_create(
                session_key=request.session.session_key,
                defaults={"user": user},
            )
        if next_url and _next_matches_account(next_url, user):
            return redirect(next_url)
        return _home_redirect(request)

    still_allowed = register_login_failure(
        actor_hint=actor_hint,
        account_name=account_name,
        client_ip=client_ip,
        secret=settings.SECRET_KEY,
        limit=settings.PLATFORM_SETTINGS.security.login_failure_limit,
        window_seconds=(
            settings.PLATFORM_SETTINGS.security
            .login_failure_window_seconds
        ),
    )
    status = 200 if still_allowed else 429
    if not still_allowed:
        form.add_error(None, "登录暂时受限，请稍后重试。")
    return render(
        request,
        "course_insight/login.html",
        {"form": form, "next": next_url},
        status=status,
    )


@login_required
@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    if request.session.session_key:
        AuthenticatedSession.objects.filter(
            session_key=request.session.session_key
        ).delete()
    django_logout(request)
    return redirect("login")


def _single_login_values(request: HttpRequest) -> bool:
    return all(
        len(request.POST.getlist(name)) == 1
        for name in ("username", "password")
    )


def _login_rate_limit_hint(account_name: object) -> str:
    """Keep legacy lock buckets while accepting unrestricted account names."""

    if (
        isinstance(account_name, str)
        and account_name
        and account_name == account_name.strip()
        and len(account_name) <= 256
        and account_name.isprintable()
    ):
        return account_name
    if isinstance(account_name, str):
        return account_name_digest(account_name)
    return "invalid-login"


def _safe_next(request: HttpRequest) -> str:
    candidate = (
        request.POST.get("next", "")
        if request.method == "POST"
        else request.GET.get("next", "")
    )
    if (
        isinstance(candidate, str)
        and len(candidate) <= 2_048
        and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return candidate
    return ""


def _home_redirect(request: HttpRequest) -> HttpResponse:
    account_type = resolve_account_type(request.user)
    if account_type == AccountType.ADMINISTRATOR:
        return redirect("account-admin-home")
    if account_type == AccountType.STUDENT:
        return redirect("student-home")
    return redirect("teacher-home")


def _next_matches_account(next_url: str, user: object) -> bool:
    account_type = resolve_account_type(user)  # type: ignore[arg-type]
    prefixes = {
        AccountType.STUDENT: "/student/",
        AccountType.TEACHER: "/teacher/",
        AccountType.ADMINISTRATOR: "/account-admin/",
    }
    return next_url.startswith(prefixes.get(account_type, "\0"))
