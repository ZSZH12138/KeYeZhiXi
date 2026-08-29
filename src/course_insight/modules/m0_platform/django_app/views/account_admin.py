"""HTML endpoints for the administrator's account-only console."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from course_insight.modules.m0_platform.django_app.account_governance import (
    create_managed_account,
)
from course_insight.modules.m0_platform.django_app.authz import (
    authorize_account_admin,
)
from course_insight.modules.m0_platform.django_app.forms.governance import (
    ManagedAccountCreationForm,
)
from course_insight.modules.m0_platform.django_app.models import AccountType, User


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
            ).order_by("actor_id"),
            "students": User.objects.filter(
                account_type=AccountType.STUDENT
            ).order_by("actor_id"),
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


def _create(request: HttpRequest, account_type: str) -> HttpResponse:
    authorize_account_admin(request.user)
    form = ManagedAccountCreationForm(
        data=request.POST if request.method == "POST" else None
    )
    if request.method == "POST":
        supplied = set(request.POST) - {"csrfmiddlewaretoken"}
        if supplied != {"actor_id", "password1", "password2"}:
            form.add_error(None, "提交字段不完整或包含未允许字段")
        elif form.is_valid():
            try:
                create_managed_account(
                    actor_id=str(form.cleaned_data["actor_id"]),
                    account_type=account_type,
                    raw_password=str(form.cleaned_data["password1"]),
                    administrator=request.user,
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                return redirect("account-admin-home")
    return render(
        request,
        "course_insight/account_admin/create.html",
        {
            "form": form,
            "account_label": "教师" if account_type == AccountType.TEACHER else "学生",
        },
        status=400 if request.method == "POST" else 200,
    )
