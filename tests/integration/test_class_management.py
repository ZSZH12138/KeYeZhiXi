from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.authz import (
    ROLE_PERMISSIONS,
)
from course_insight.modules.m0_platform.django_app.class_management import (
    add_student,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ActorGrant,
    ClassMembership,
    CourseClassWorkspace,
    RoleName,
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


def _canonical_teacher(actor_id: str) -> User:
    """Create a legacy teacher whose group is built from the canonical map."""

    user = User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        account_type=AccountType.TEACHER,
        password="Strong-password-123!",
    )
    group, _ = Group.objects.get_or_create(name=RoleName.TEACHER)
    group.permissions.set(
        Permission.objects.filter(
            content_type__app_label="m0_platform_web",
            codename__in=ROLE_PERMISSIONS[RoleName.TEACHER],
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


def test_teacher_opens_owned_class_lookup_with_display_names() -> None:
    teacher = _teacher("pseudonym_teacher_display_scope_lookup")
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_opaque_scope_lookup",
        class_id="class_opaque_scope_lookup",
        course_display_name="computer_network",
        class_display_name="class1",
        owner_teacher=teacher,
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse("teacher-class-lookup"),
        {"course_id": "computer_network", "class_id": "class1"},
    )

    assert response.status_code == 302
    assert response.url == reverse(
        "teacher-class",
        kwargs={
            "course_id": workspace.course_id,
            "class_id": workspace.class_id,
        },
    )


def test_teacher_pages_render_readable_scope_and_student_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opaque scope and actor IDs stay out of teacher-visible labels."""

    teacher = _teacher("pseudonym_teacher_readable_inputs")
    student = User.objects.create_user(
        username="student_demo1",
        actor_id="pseudonym_student_readable_inputs",
        account_type=AccountType.STUDENT,
    )
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_opaque_readable_inputs",
        class_id="class_opaque_readable_inputs",
        course_display_name="computer_network",
        class_display_name="class1",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: _DynamicRuntime())
    client = Client()
    client.force_login(teacher)

    home = client.get(reverse("teacher-home"))
    class_page = client.get(
        reverse(
            "teacher-class",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
            },
        )
    )

    assert home.status_code == 200
    home_text = strip_tags(home.content.decode())
    assert "computer_network" in home_text
    assert "class1" in home_text
    assert "Course id" not in home_text
    assert "Class id" not in home_text
    assert class_page.status_code == 200
    class_text = strip_tags(class_page.content.decode())
    assert "课程：computer_network；班级：class1" in class_text
    assert "student_demo1" in class_text
    assert "Course id" not in class_text
    assert "Class id" not in class_text
    assert "Learner account" not in class_text
    assert "pseudonym_student_readable_inputs" not in class_text


def test_teacher_review_lookup_accepts_current_class_membership() -> None:
    teacher = _teacher("pseudonym_teacher_membership_review_lookup")
    student = _student("pseudonym_student_membership_review_lookup")
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_membership_review_lookup",
        class_id="class_membership_review_lookup",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse("teacher-review-lookup"),
        {
            "course_id": workspace.course_id,
            "class_id": workspace.class_id,
            "learner_account": student.username,
        },
    )

    assert response.status_code == 302
    assert response.url == reverse(
        "teacher-learner-review-list",
        kwargs={
            "course_id": workspace.course_id,
            "class_id": workspace.class_id,
            "learner_id": student.actor_id,
        },
    )


def test_removed_owned_class_membership_cannot_fall_back_to_legacy_grant() -> None:
    teacher = _teacher("pseudonym_teacher_removed_membership_review")
    student = _student("pseudonym_student_removed_membership_review")
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_removed_membership_review",
        class_id="class_removed_membership_review",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
        status=ClassMembership.Status.REMOVED,
        removed_at=timezone.now(),
        removed_by_teacher=teacher,
    )
    ActorGrant.objects.create(
        user=student,
        role=RoleName.STUDENT,
        course_id=workspace.course_id,
        class_id=workspace.class_id,
        source_checksum="a" * 64,
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse("teacher-review-lookup"),
        {
            "course_id": workspace.course_id,
            "class_id": workspace.class_id,
            "learner_account": student.username,
        },
    )

    assert response.status_code == 404


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
        {"student_account": student.username},
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
    assert student.username in body
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
                "student_id": student.pk,
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


def test_canonical_teacher_group_can_open_and_manage_own_class() -> None:
    """Role-sync-created teacher accounts must not lose new class controls."""

    teacher = _canonical_teacher("pseudonym_teacher_canonical_group")
    student = _student("pseudonym_student_canonical_group")
    client = Client()
    client.force_login(teacher)

    opened = client.post(
        reverse("teacher-open-class"),
        {
            "course_name": "数据结构",
            "class_name": "一班",
            "request_token": "open-class-canonical-group-001",
        },
    )

    assert opened.status_code == 302
    workspace = CourseClassWorkspace.objects.get(owner_teacher=teacher)
    added = client.post(
        reverse(
            "teacher-class-add-student",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
            },
        ),
        {"student_account": student.username},
    )
    assert added.status_code == 302
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
    assert removed.status_code == 302


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


def test_student_cannot_use_open_class_endpoint() -> None:
    student = _student("pseudonym_student_cannot_open")
    client = Client()
    client.force_login(student)

    response = client.post(
        reverse("teacher-open-class"),
        {"unexpected": "value"},
    )

    assert response.status_code == 403
    assert not CourseClassWorkspace.objects.exists()


def test_class_governance_rejects_extra_post_fields() -> None:
    teacher = _teacher("pseudonym_teacher_strict_fields")
    student = _student("pseudonym_student_strict_fields")
    client = Client()
    client.force_login(teacher)

    opened = client.post(
        reverse("teacher-open-class"),
        {
            "course_name": "数据结构",
            "class_name": "一班",
            "request_token": "open-class-request-strict-001",
            "owner_teacher": student.actor_id,
        },
    )

    assert opened.status_code == 400
    assert not CourseClassWorkspace.objects.exists()

    workspace = CourseClassWorkspace.objects.create(
        course_id="course_strict_fields",
        class_id="class_strict_fields",
        owner_teacher=teacher,
    )
    added = client.post(
        reverse(
            "teacher-class-add-student",
            kwargs={
                "course_id": workspace.course_id,
                "class_id": workspace.class_id,
            },
        ),
        {
            "student_account": student.actor_id,
            "status": ClassMembership.Status.ACTIVE,
        },
    )

    assert added.status_code == 400
    assert not ClassMembership.objects.filter(
        workspace=workspace,
        student=student,
    ).exists()


def test_teacher_home_uses_workspace_names_for_knowledge_and_deepseek_links() -> None:
    """Opaque scope identifiers must never become the teacher-facing labels."""

    teacher = _teacher("pseudonym_teacher_named_workspace_links")
    CourseClassWorkspace.objects.create(
        course_id="course_opaque_link",
        class_id="class_opaque_link",
        course_display_name="数据结构",
        class_display_name="一班",
        owner_teacher=teacher,
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(reverse("teacher-home"))

    body = response.content.decode()
    assert response.status_code == 200
    assert "数据结构 / 一班" in body
    assert "管理 数据结构 / 一班" in body
    assert "配置 数据结构 / 一班" in body
    assert "管理 course_opaque_link / class_opaque_link" not in body
    assert "配置 course_opaque_link / class_opaque_link" not in body


def test_teacher_home_gives_legacy_workspace_a_readable_label() -> None:
    """A migrated workspace without old display fields must not render as '/'."""

    teacher = _teacher("pseudonym_teacher_legacy_workspace_label")
    CourseClassWorkspace.objects.create(
        course_id="course_network",
        class_id="class_01",
        owner_teacher=teacher,
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(reverse("teacher-home"))

    body = response.content.decode()
    assert response.status_code == 200
    assert "计算机网络核心原理与故障诊断 / 01班" in body


def test_student_selects_an_invited_workspace_by_its_display_names() -> None:
    """Students choose a class label, while the server keeps opaque IDs internal."""

    teacher = _teacher("pseudonym_teacher_student_workspace_picker")
    student = _student("pseudonym_student_workspace_picker")
    group, _ = Group.objects.get_or_create(name="student-workspace-picker")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="start_assessment",
        )
    )
    student.groups.add(group)
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_opaque_student",
        class_id="class_opaque_student",
        course_display_name="操作系统",
        class_display_name="二班",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(workspace=workspace, student=student)
    client = Client()
    client.force_login(student)

    home = client.get(reverse("student-home"))
    selection = client.get(
        reverse("student-select-workspace"),
        {"workspace_id": str(workspace.pk)},
    )

    body = home.content.decode()
    assert home.status_code == 200
    assert "操作系统 / 二班" in body
    assert "course_opaque_student / class_opaque_student" not in body
    assert selection.status_code == 302
    assert selection.url == reverse(
        "student-start",
        kwargs={
            "course_id": workspace.course_id,
            "class_id": workspace.class_id,
        },
    )


def test_student_workspace_selection_rejects_a_class_without_membership() -> None:
    """A posted workspace ID cannot bypass the student's class membership."""

    teacher = _teacher("pseudonym_teacher_workspace_picker_denied")
    student = _student("pseudonym_student_workspace_picker_denied")
    group, _ = Group.objects.get_or_create(name="student-workspace-picker-denied")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="start_assessment",
        )
    )
    student.groups.add(group)
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_opaque_denied",
        class_id="class_opaque_denied",
        course_display_name="数据库",
        class_display_name="三班",
        owner_teacher=teacher,
    )
    client = Client()
    client.force_login(student)

    response = client.get(
        reverse("student-select-workspace"),
        {"workspace_id": str(workspace.pk)},
    )

    assert response.status_code == 400
