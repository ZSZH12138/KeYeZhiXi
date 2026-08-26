from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from course_insight.modules.m0_platform.django_app.models import ActorGrant, User
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    resolve_scoped_deepseek_settings,
    save_scoped_deepseek_settings,
    scoped_deepseek_status,
)


pytestmark = pytest.mark.django_db


def _teacher() -> User:
    return User.objects.create_user(
        username="pseudonym_teacher_scoped_key",
        actor_id="pseudonym_teacher_scoped_key",
    )


def test_scoped_key_is_encrypted_masked_and_class_isolated() -> None:
    teacher = _teacher()
    status = save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-deepseek-private-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=teacher,
    )

    from course_insight.modules.m0_platform.django_app.models import (
        ScopedDeepSeekConfiguration,
    )

    row = ScopedDeepSeekConfiguration.objects.get()
    assert b"sk-deepseek-private-test-key" not in bytes(row.encrypted_api_key)
    assert status.configured is True
    assert status.masked_key == "sk-d••••-key"
    assert status.api_revision == 1
    assert resolve_scoped_deepseek_settings(
        "course_1", "class_1"
    ).api_key == "sk-deepseek-private-test-key"
    assert resolve_scoped_deepseek_settings("course_1", "class_2") is None
    assert scoped_deepseek_status("course_1", "class_2").configured is False


def test_clearing_scoped_key_disables_only_that_workspace() -> None:
    teacher = _teacher()
    for class_id in ("class_1", "class_2"):
        save_scoped_deepseek_settings(
            course_id="course_1",
            class_id=class_id,
            api_key=f"sk-private-{class_id}",
            model_name="deepseek-v4-flash",
            thinking_enabled=False,
            updated_by=teacher,
        )

    cleared = save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key=None,
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        clear_key=True,
        updated_by=teacher,
    )

    assert cleared.configured is False
    assert cleared.api_revision == 2
    assert resolve_scoped_deepseek_settings("course_1", "class_1") is None
    assert resolve_scoped_deepseek_settings("course_1", "class_2") is not None


def test_teacher_page_saves_key_for_the_exact_authorized_class(client: Client) -> None:
    teacher = _teacher()
    group = Group.objects.create(name="scoped-key-teacher")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="configure_deepseek",
        )
    )
    teacher.groups.add(group)
    ActorGrant.objects.create(
        user=teacher,
        role="teacher",
        course_id="course_1",
        class_id="class_1",
        source_checksum="a" * 64,
    )
    client.force_login(teacher)

    response = client.post(
        reverse(
            "teacher-deepseek-settings",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {
            "api_key": "sk-page-private-key",
            "model_name": "deepseek-v4-flash",
        },
        follow=True,
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "sk-page-private-key" not in body
    assert resolve_scoped_deepseek_settings(
        "course_1", "class_1"
    ).api_key == "sk-page-private-key"
    assert resolve_scoped_deepseek_settings("course_1", "class_2") is None
