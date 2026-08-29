from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.class_management import (
    add_student,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    User,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    ensure_current_class_learning_snapshot,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _teacher(actor_id: str) -> User:
    user = User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        account_type=AccountType.TEACHER,
        password="Strong-password-123!",
    )
    codenames = {
        "view_class_analytics",
        "view_student_report",
        "review_score",
        "configure_deepseek",
        "manage_course_knowledge",
        "open_class",
        "manage_class_members",
    }
    group, _ = Group.objects.get_or_create(name=f"teacher_{actor_id}")
    group.permissions.add(
        *Permission.objects.filter(
            content_type__app_label="m0_platform_web",
            codename__in=codenames,
        )
    )
    user.groups.add(group)
    return user


def _student(actor_id: str) -> User:
    return User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        account_type=AccountType.STUDENT,
    )


class _DynamicRuntime:
    container = SimpleNamespace(m9_service=None)

    @staticmethod
    def require_course(course_id: str) -> object:
        config_dir = Path(settings.PLATFORM_SETTINGS.config_dir)
        return SimpleNamespace(
            course_id=course_id,
            course_context=None,
            state_policy_path=config_dir / "state.json",
            teacher_threshold_policy_path=config_dir / "teacher.json",
        )


def test_teacher_home_adds_open_class_without_removing_existing_features() -> None:
    teacher = _teacher("pseudonym_teacher_home_cards")
    client = Client()
    client.force_login(teacher)

    page = client.get(reverse("teacher-home")).content.decode()

    for label in (
        "开课",
        "班级分析",
        "成绩复核",
        "课程知识与题目文件",
        "DeepSeek 接口",
    ):
        assert label in page


def test_teacher_creates_class_and_manages_student_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = _teacher("pseudonym_teacher_roster_owner")
    student = _student("pseudonym_student_roster_member")
    client = Client()
    client.force_login(teacher)

    opened = client.post(
        reverse("teacher-open-class"),
        {
            "course_name": " 数据结构 ",
            "class_name": " 一班 ",
            "request_token": "open-class-request-fixed-001",
        },
    )
    workspace = CourseClassWorkspace.objects.get(owner_teacher=teacher)
    assert opened.status_code == 302
    assert workspace.course_display_name == "数据结构"
    assert workspace.class_display_name == "一班"

    added = client.post(
        reverse(
            "teacher-class-add-student",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
            },
        ),
        {"student_account": student.actor_id},
    )
    assert added.status_code == 302
    assert ClassMembership.objects.filter(
        workspace=workspace,
        student=student,
        status=ClassMembership.Status.ACTIVE,
    ).count() == 1

    monkeypatch.setattr(runtime, "get_web_runtime", lambda: _DynamicRuntime())
    page = client.get(
        reverse(
            "teacher-class",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
            },
        )
    )
    body = page.content.decode()
    assert page.status_code == 200, body
    assert "当前学生：1 人" in body
    assert student.actor_id in body
    snapshot = ensure_current_class_learning_snapshot(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    )
    assert snapshot is not None
    assert snapshot.active_student_count == 1

    removed = client.post(
        reverse(
            "teacher-class-remove-student",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
                "actor_id": student.actor_id,
            },
        )
    )
    assert removed.status_code == 302
    assert not ClassMembership.objects.filter(
        workspace=workspace,
        student=student,
        status=ClassMembership.Status.ACTIVE,
    ).exists()
    repaired = ensure_current_class_learning_snapshot(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    )
    assert repaired is not None
    assert repaired.active_student_count == 0


def test_teacher_cannot_manage_another_teachers_class() -> None:
    owner = _teacher("pseudonym_teacher_class_owner")
    outsider = _teacher("pseudonym_teacher_class_outsider")
    student = _student("pseudonym_student_cross_owner")
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_owned",
        class_id="class_owned",
        owner_teacher=owner,
    )

    with pytest.raises(PermissionDenied):
        add_student(
            workspace=workspace,
            teacher=outsider,
            actor_id=student.actor_id,
        )
