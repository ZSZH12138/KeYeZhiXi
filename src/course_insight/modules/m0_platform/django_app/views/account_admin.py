"""HTML endpoints for the administrator's account-only console."""

from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from course_insight.modules.m0_platform.django_app.account_governance import (
    create_managed_account,
    erase_managed_account,
    preview_account_erasure,
)
from course_insight.modules.m0_platform.django_app.authz import (
    authorize_account_admin,
)
from course_insight.modules.m0_platform.django_app.forms.governance import (
    AccountDeletionConfirmationForm,
    ManagedAccountCreationForm,
)
from course_insight.modules.m0_platform.django_app.models import AccountType, User


logger = logging.getLogger(__name__)


@login_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    authorize_account_admin(request.user)
    return render(
        request,
        "course_insight/account_admin/home.html",
        {
            "teachers": User.objects.filter(
                account_type=AccountType.TEACHER
            ).order_by("username"),
            "students": User.objects.filter(
                account_type=AccountType.STUDENT
            ).order_by("username"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_teacher(request: HttpRequest) -> HttpResponse:
    return _create(request, AccountType.TEACHER)


@login_required
@require_http_methods(["GET", "POST"])
def create_student(request: HttpRequest) -> HttpResponse:
    return _create(request, AccountType.STUDENT)


@login_required
@require_http_methods(["GET", "POST"])
def delete_teacher(request: HttpRequest, user_id: int) -> HttpResponse:
    return _delete(request, user_id, AccountType.TEACHER)


@login_required
@require_http_methods(["GET", "POST"])
def delete_student(request: HttpRequest, user_id: int) -> HttpResponse:
    return _delete(request, user_id, AccountType.STUDENT)


def _create(request: HttpRequest, account_type: str) -> HttpResponse:
    authorize_account_admin(request.user)
    form = ManagedAccountCreationForm(
        data=request.POST if request.method == "POST" else None
    )
    response_status = 400 if request.method == "POST" else 200
    if request.method == "POST":
        supplied = set(request.POST) - {"csrfmiddlewaretoken"}
        if supplied != {"account_name", "password1", "password2"} or not all(
            len(request.POST.getlist(name)) == 1 for name in supplied
        ):
            form.add_error(None, "提交字段不完整或包含未允许字段")
        elif form.is_valid():
            try:
                create_managed_account(
                    account_name=str(form.cleaned_data["account_name"]),
                    account_type=account_type,
                    raw_password=str(form.cleaned_data["password1"]),
                    administrator=request.user,
                )
            except ValidationError as error:
                form.add_error(None, error)
            except OperationalError as error:
                if not _is_sqlite_lock_error(error):
                    raise
                logger.warning(
                    "account creation temporarily blocked by SQLite writer",
                    extra={"account_type": account_type},
                )
                form.add_error(None, "账户创建暂时繁忙，请稍后重试。")
                response_status = 503
            else:
                return redirect("account-admin-home")
    return render(
        request,
        "course_insight/account_admin/create.html",
        {
            "form": form,
            "account_label": "教师" if account_type == AccountType.TEACHER else "学生",
        },
        status=response_status,
    )


def _is_sqlite_lock_error(error: OperationalError) -> bool:
    """Recognize the only transient database failure shown to administrators."""

    if connection.vendor != "sqlite":
        return False
    message = str(error).casefold()
    return "database is locked" in message or "database schema is locked" in message


def _delete(
    request: HttpRequest,
    user_id: int,
    account_type: str,
) -> HttpResponse:
    authorize_account_admin(request.user)
    try:
        preview = preview_account_erasure(
            user_id=user_id,
            expected_type=account_type,
        )
    except ValidationError:
        return HttpResponse("账户不存在或类型不匹配", status=404)
    form = AccountDeletionConfirmationForm(
        data=request.POST if request.method == "POST" else None
    )
    if request.method == "POST":
        supplied = set(request.POST) - {"csrfmiddlewaretoken"}
        if supplied != {"confirmed_account_name"} or not all(
            len(request.POST.getlist(name)) == 1 for name in supplied
        ):
            form.add_error(None, "提交字段不完整或包含未允许字段")
        elif form.is_valid():
            try:
                erase_managed_account(
                    user_id=user_id,
                    expected_type=account_type,
                    confirmed_account_name=str(
                        form.cleaned_data["confirmed_account_name"]
                    ),
                    administrator=request.user,
                )
            except ValidationError as error:
                form.add_error(None, error)
            except Exception:
                logger.exception(
                    "managed account erasure failed",
                    extra={"account_type": account_type},
                )
                form.add_error(
                    None,
                    "注销未完成，目标账户未被删除。请稍后重试；若持续失败，请检查服务日志。",
                )
            else:
                return redirect("account-admin-home")
    return render(
        request,
        "course_insight/account_admin/delete.html",
        {"form": form, "preview": preview},
        status=400 if request.method == "POST" else 200,
    )
