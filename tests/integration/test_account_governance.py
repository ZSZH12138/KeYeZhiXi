from __future__ import annotations

import django
import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse


if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)
APP_LABEL = "m0_platform_web"


def _user(actor_id: str, account_type: str) -> User:
    return User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        account_type=account_type,
        password="Strong-password-123!",
    )


def _permission(user: User, codename: str) -> None:
    group, _ = Group.objects.get_or_create(name=f"test_{codename}")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label=APP_LABEL,
            codename=codename,
        )
    )
    user.groups.add(group)


def test_governance_models_enforce_one_account_type_and_active_membership() -> None:
    teacher = _user("pseudonym_teacher_schema", AccountType.TEACHER)
    student = _user("pseudonym_student_schema", AccountType.STUDENT)
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_schema",
        class_id="class_schema",
        course_display_name="课程",
        class_display_name="一班",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClassMembership.objects.create(
                workspace=workspace,
                student=student,
                added_by_teacher=teacher,
            )

    assert workspace.owner_teacher_id == teacher.pk
    assert workspace.course_display_name == "课程"
    assert workspace.class_display_name == "一班"


def test_account_admin_creates_teacher_with_hashed_password() -> None:
    administrator = _user(
        "pseudonym_admin_create",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")
    client = Client()
    client.force_login(administrator)

    response = client.post(
        reverse("account-admin-create-teacher"),
        {
            "actor_id": "pseudonym_teacher_created",
            "password1": "Strong-password-123!",
            "password2": "Strong-password-123!",
        },
    )

    created = User.objects.get(actor_id="pseudonym_teacher_created")
    assert response.status_code == 302
    assert response.url == reverse("account-admin-home")
    assert created.account_type == AccountType.TEACHER
    assert created.check_password("Strong-password-123!")


@pytest.mark.parametrize(
    ("account_type", "permission", "home_name"),
    [
        (AccountType.STUDENT, "start_assessment", "student-home"),
        (AccountType.TEACHER, "view_class_analytics", "teacher-home"),
        (AccountType.ADMINISTRATOR, "manage_accounts", "account-admin-home"),
    ],
)
def test_login_redirects_to_single_account_home(
    account_type: str,
    permission: str,
    home_name: str,
) -> None:
    user = _user(f"pseudonym_login_{account_type}", account_type)
    _permission(user, permission)

    response = Client().post(
        reverse("login"),
        {"username": user.actor_id, "password": "Strong-password-123!"},
    )

    assert response.status_code == 302
    assert response.url == reverse(home_name)
