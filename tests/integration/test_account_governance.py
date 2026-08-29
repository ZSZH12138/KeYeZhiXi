from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import django
import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse


if not apps.ready:
    django.setup()

from course_insight.application.actor_erasure import ActorErasureResult  # noqa: E402
from course_insight.contracts.tasking import TaskPlan  # noqa: E402
from course_insight.modules.m0_platform.django_app import (  # noqa: E402
    account_governance,
)
from course_insight.modules.m0_platform.django_app import (  # noqa: E402
    assessment_evidence,
)
from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    DeletedActorFingerprint,
    ScopedDeepSeekConfiguration,
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


def test_administrator_cannot_enter_teacher_or_student_home_even_with_permissions() -> None:
    administrator = _user(
        "pseudonym_admin_business_denied",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "start_assessment")
    _permission(administrator, "view_class_analytics")
    client = Client()
    client.force_login(administrator)

    assert client.get(reverse("student-home")).status_code == 403
    assert client.get(reverse("teacher-home")).status_code == 403


def test_dynamic_course_builds_release_evidence_without_static_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = TaskPlan(
        task_id="task_dynamic_evidence",
        task_type="practice",
        course_id="course_dynamic_evidence",
        class_id="class_dynamic_evidence",
        learner_id="pseudonym_student_dynamic_evidence",
        session_id="session_dynamic_evidence",
        blueprint_id="blueprint_dynamic_evidence",
        knowledge_bundle_id="release_dynamic_evidence",
        course_package_id="package_dynamic_evidence",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=datetime.now(UTC),
    )
    built_index = SimpleNamespace(course_package_id="package_dynamic_evidence")
    builder = SimpleNamespace(build_index=lambda package: built_index)
    web_runtime = SimpleNamespace(
        container=SimpleNamespace(m2_service=builder),
    )
    monkeypatch.setattr(
        assessment_evidence,
        "_release_for_task",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        assessment_evidence,
        "course_package_from_release",
        lambda release: object(),
    )

    resolved = assessment_evidence.evidence_index_for_assessment(
        web_runtime,
        SimpleNamespace(course_context=None),
        task=task,
        knowledge_bundle=SimpleNamespace(
            course_package_id="package_dynamic_evidence"
        ),
        course_id=task.course_id,
        class_id=task.class_id,
    )

    assert resolved is built_index


def test_student_deletion_physically_removes_account_and_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _user(
        "pseudonym_admin_delete_student",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")
    teacher = _user(
        "pseudonym_teacher_delete_student",
        AccountType.TEACHER,
    )
    student = _user(
        "pseudonym_student_delete_target",
        AccountType.STUDENT,
    )
    actor_id = student.actor_id
    student_pk = student.pk
    student_client = Client()
    student_client.force_login(student)
    student_session_key = student_client.session.session_key
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_delete_student",
        class_id="class_delete_student",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
    )
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda value: ActorErasureResult(module_counts=(("m0", 1),)),
    )
    client = Client()
    client.force_login(administrator)

    response = client.post(
        reverse(
            "account-admin-delete-student",
            kwargs={"actor_id": actor_id},
        ),
        {"confirmed_actor_id": actor_id},
    )

    assert response.status_code == 302
    assert not User.objects.filter(pk=student_pk).exists()
    assert not ClassMembership.objects.filter(student_id=student_pk).exists()
    assert not Session.objects.filter(session_key=student_session_key).exists()
    assert DeletedActorFingerprint.objects.filter(
        target_digest=account_governance.actor_fingerprint(actor_id)
    ).exists()


def test_teacher_deletion_archives_owned_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _user(
        "pseudonym_admin_delete_teacher",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")
    teacher = _user(
        "pseudonym_teacher_delete_target",
        AccountType.TEACHER,
    )
    actor_id = teacher.actor_id
    teacher_pk = teacher.pk
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_delete_teacher",
        class_id="class_delete_teacher",
        owner_teacher=teacher,
    )
    ScopedDeepSeekConfiguration.objects.create(
        workspace=workspace,
        updated_by=teacher,
    )
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda value: ActorErasureResult(module_counts=()),
    )
    client = Client()
    client.force_login(administrator)

    response = client.post(
        reverse(
            "account-admin-delete-teacher",
            kwargs={"actor_id": actor_id},
        ),
        {"confirmed_actor_id": actor_id},
    )

    assert response.status_code == 302
    workspace.refresh_from_db()
    assert not User.objects.filter(pk=teacher_pk).exists()
    assert workspace.owner_teacher_id is None
    assert workspace.status == CourseClassWorkspace.Status.ARCHIVED
    assert not ScopedDeepSeekConfiguration.objects.filter(
        workspace=workspace
    ).exists()


def test_failed_runtime_erasure_freezes_account_and_returns_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _user(
        "pseudonym_admin_delete_failure",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")
    student = _user(
        "pseudonym_student_delete_failure",
        AccountType.STUDENT,
    )
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda value: (_ for _ in ()).throw(RuntimeError("database detail")),
    )
    client = Client()
    client.force_login(administrator)
    client.raise_request_exception = False

    response = client.post(
        reverse(
            "account-admin-delete-student",
            kwargs={"actor_id": student.actor_id},
        ),
        {"confirmed_actor_id": student.actor_id},
    )

    student.refresh_from_db()
    assert response.status_code == 400
    assert "注销未完成，目标账户已冻结" in response.content.decode()
    assert student.is_active is False
