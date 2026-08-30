from __future__ import annotations

import os

import django
import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import NoReverseMatch, reverse


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    ActorGrant,
    ClassMembership,
    CourseClassWorkspace,
    RoleName,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _grant_permission(
    user: User,
    *,
    role: RoleName,
    codename: str,
) -> None:
    group, _ = Group.objects.get_or_create(name=role)
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename=codename,
        )
    )
    user.groups.add(group)


def _user(actor_id: str) -> User:
    user = User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        password="Test-password-123!",
    )
    user.email = ""
    user.first_name = ""
    user.last_name = ""
    user.save(
        update_fields=["email", "first_name", "last_name", "password"]
    )
    return user


def test_login_page_renders_without_template_errors(client: Client) -> None:
    response = client.get(reverse("login"))

    assert response.status_code == 200
    assert any(
        template.name == "course_insight/login.html"
        for template in response.templates
        if template.name
    )
    assert "username" in response.content.decode("utf-8")


def test_student_home_renders_for_authorized_student(
    client: Client,
) -> None:
    user = _user("pseudonym_student_web_001")
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_demo",
        class_id="class_01",
        course_display_name="课程演示",
        class_display_name="01班",
    )
    ClassMembership.objects.create(workspace=workspace, student=user)
    _grant_permission(
        user,
        role=RoleName.STUDENT,
        codename="start_assessment",
    )
    client.force_login(user)

    response = client.get(reverse("student-home"))

    assert response.status_code == 200
    assert any(
        template.name == "course_insight/student/home.html"
        for template in response.templates
        if template.name
    )
    page = response.content.decode("utf-8")
    assert "课程演示 / 01班" in page
    assert "workspace_id" in page
    assert "使用课程和班级编号" not in page
    assert "course_id" not in page
    assert "class_id" not in page
    with pytest.raises(NoReverseMatch):
        reverse("student-select-scope")


def test_teacher_home_renders_for_authorized_teacher(
    client: Client,
) -> None:
    user = _user("pseudonym_teacher_web_001")
    ActorGrant.objects.create(
        user=user,
        role=RoleName.TEACHER,
        course_id="course_demo",
        class_id="class_01",
        is_active=True,
        source_checksum="b" * 64,
    )
    _grant_permission(
        user,
        role=RoleName.TEACHER,
        codename="view_class_analytics",
    )
    client.force_login(user)

    response = client.get(reverse("teacher-home"))

    assert response.status_code == 200
    assert any(
        template.name == "course_insight/teacher/home.html"
        for template in response.templates
        if template.name
    )
    page = response.content.decode("utf-8")
    assert "learner_account" in page
    assert "course_id" in page
    assert "查看" in page
