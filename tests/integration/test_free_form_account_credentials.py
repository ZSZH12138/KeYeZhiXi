from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from course_insight.application.actor_erasure import ActorErasureResult
from course_insight.modules.m0_platform.django_app import account_governance
from course_insight.modules.m0_platform.django_app.forms.auth import (
    AccountAuthenticationForm,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    LoginFailureBucket,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)
_ACTOR_PATTERN = re.compile(r"^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$")


def _administrator() -> User:
    administrator = User.objects.create_user(
        username="pseudonym_free_form_administrator",
        actor_id="pseudonym_free_form_administrator",
        account_type=AccountType.ADMINISTRATOR,
        password="administrator-password",
    )
    group, _ = Group.objects.get_or_create(name="free_form_account_admin")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="manage_accounts",
        )
    )
    administrator.groups.add(group)
    return administrator


def _managed_account(
    *,
    account_name: str,
    account_type: str,
    administrator: User,
) -> User:
    return account_governance.create_managed_account(
        account_name=account_name,
        account_type=account_type,
        raw_password="  密码 / 保留空格  ",
        administrator=administrator,
    )


def test_login_form_does_not_render_a_username_length_limit() -> None:
    form = AccountAuthenticationForm()

    assert form.fields["username"].max_length is None
    assert "maxlength" not in form.fields["username"].widget.attrs


def test_admin_can_create_and_login_with_a_free_form_long_account_name() -> None:
    administrator = _administrator()
    account_name = "  教师 / 特殊名称? " + "甲" * 1_024
    password = "  密码 / 保留空格  "
    client = Client()
    client.force_login(administrator)

    created_response = client.post(
        reverse("account-admin-create-teacher"),
        {
            "account_name": account_name,
            "password1": password,
            "password2": password,
        },
    )

    teacher = User.objects.get(username=account_name)
    login_response = Client().post(
        reverse("login"),
        {"username": account_name, "password": password},
    )
    assert created_response.status_code == 302
    assert teacher.account_type == AccountType.TEACHER
    assert teacher.username == account_name
    assert teacher.actor_id != account_name
    assert _ACTOR_PATTERN.fullmatch(teacher.actor_id)
    assert teacher.check_password(password)
    assert login_response.status_code == 302
    assert login_response.url == reverse("teacher-home")


@pytest.mark.parametrize(
    ("account_name", "password", "field_name"),
    [
        ("\t \n", "valid", "account_name"),
        ("valid", "\t \n", "password1"),
    ],
)
def test_account_creation_rejects_only_blank_credentials(
    account_name: str,
    password: str,
    field_name: str,
) -> None:
    administrator = _administrator()
    client = Client()
    client.force_login(administrator)

    response = client.post(
        reverse("account-admin-create-student"),
        {
            "account_name": account_name,
            "password1": password,
            "password2": password,
        },
    )

    assert response.status_code == 400
    assert field_name in response.context["form"].errors


def test_teacher_can_invite_and_remove_a_special_character_student_account() -> None:
    administrator = _administrator()
    teacher = _managed_account(
        account_name="教师 / A",
        account_type=AccountType.TEACHER,
        administrator=administrator,
    )
    student = _managed_account(
        account_name="学生 / B?",
        account_type=AccountType.STUDENT,
        administrator=administrator,
    )
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_free_form",
        class_id="class_free_form",
        owner_teacher=teacher,
    )
    client = Client()
    client.force_login(teacher)

    invited = client.post(
        reverse(
            "teacher-class-add-student",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
            },
        ),
        {"student_account": student.username},
    )
    removed = client.post(
        reverse(
            "teacher-class-remove-student",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
                "student_id": student.pk,
            },
        ),
    )

    assert invited.status_code == 302
    assert removed.status_code == 302
    assert not ClassMembership.objects.filter(
        workspace=workspace,
        student=student,
        status=ClassMembership.Status.ACTIVE,
    ).exists()


def test_admin_can_physically_delete_a_special_character_account_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _administrator()
    student = _managed_account(
        account_name="学生 / 待注销",
        account_type=AccountType.STUDENT,
        administrator=administrator,
    )
    student_id = student.pk
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda actor_id: ActorErasureResult(module_counts=(("m0", 1),)),
    )
    client = Client()
    client.force_login(administrator)

    response = client.post(
        reverse(
            "account-admin-delete-student",
            kwargs={"user_id": student_id},
        ),
        {"confirmed_account_name": student.username},
    )

    assert response.status_code == 302
    assert not User.objects.filter(pk=student_id).exists()


def test_physical_delete_removes_login_buckets_for_a_long_account_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _administrator()
    account_name = "  学生 / 超长账户  " + "甲" * 300
    student = _managed_account(
        account_name=account_name,
        account_type=AccountType.STUDENT,
        administrator=administrator,
    )
    student_id = student.pk
    failed_login = Client().post(
        reverse("login"),
        {"username": account_name, "password": "错误密码"},
        REMOTE_ADDR="192.0.2.25",
    )
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda actor_id: ActorErasureResult(module_counts=(("m0", 1),)),
    )
    administrator_client = Client()
    administrator_client.force_login(administrator)

    deleted = administrator_client.post(
        reverse(
            "account-admin-delete-student",
            kwargs={"user_id": student_id},
        ),
        {"confirmed_account_name": account_name},
    )

    assert failed_login.status_code == 200
    assert deleted.status_code == 302
    assert not User.objects.filter(pk=student_id).exists()
    remaining_buckets = tuple(LoginFailureBucket.objects.all())
    assert len(remaining_buckets) == 1
    assert remaining_buckets[0].account_user_id is None
