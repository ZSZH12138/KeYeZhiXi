from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import django
import pytest
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import Client, override_settings
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
from course_insight.modules.m0_platform.django_app.views import (  # noqa: E402
    account_admin,
)
from course_insight.modules.m0_platform.django_app.authz import (  # noqa: E402
    is_login_allowed,
    register_login_failure,
)
from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    AccountLifecycleEvent,
    AccountType,
    ActorGrant,
    ClassLearningSnapshot,
    ClassMembership,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    ErasureFileCleanup,
    KnowledgeIngestionJob,
    RoleName,
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
            "account_name": "teacher / created?",
            "password1": "!",
            "password2": "!",
        },
    )

    created = User.objects.get(username="teacher / created?")
    assert response.status_code == 302
    assert response.url == reverse("account-admin-home")
    assert created.account_type == AccountType.TEACHER
    assert created.actor_id != created.username
    assert created.check_password("!")


def test_sqlite_uses_wal_safe_immediate_transactions_for_account_writes() -> None:
    options = settings.DATABASES["default"]["OPTIONS"]

    assert options["transaction_mode"] == "IMMEDIATE"
    assert options["timeout"] >= 30.0
    assert "journal_mode=WAL" in options["init_command"]
    assert "busy_timeout=30000" in options["init_command"]


def test_account_creation_hides_exhausted_sqlite_lock_from_administrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _user(
        "pseudonym_admin_account_create_lock",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")

    def fail_with_sqlite_lock(**kwargs: object) -> User:
        del kwargs
        raise OperationalError("database is locked")

    monkeypatch.setattr(
        account_admin,
        "create_managed_account",
        fail_with_sqlite_lock,
    )
    client = Client()
    client.force_login(administrator)
    client.raise_request_exception = False

    response = client.post(
        reverse("account-admin-create-teacher"),
        {
            "account_name": "teacher under lock",
            "password1": "password",
            "password2": "password",
        },
    )

    body = response.content.decode("utf-8")
    assert response.status_code == 503
    assert "账户创建暂时繁忙，请稍后重试。" in body
    assert "database is locked" not in body


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
            kwargs={"user_id": student.pk},
        ),
        {"confirmed_account_name": student.username},
    )

    assert response.status_code == 302
    assert not User.objects.filter(pk=student_pk).exists()
    assert not ClassMembership.objects.filter(student_id=student_pk).exists()
    assert not Session.objects.filter(session_key=student_session_key).exists()
    assert not AccountLifecycleEvent.objects.filter(
        target_user_id=student_pk
    ).exists()


def test_deleted_account_name_can_be_reused_without_inheriting_login_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    administrator = _user(
        "pseudonym_admin_reuse_deleted_name",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")
    account_name = "reusable deleted student"
    student = account_governance.create_managed_account(
        account_name=account_name,
        account_type=AccountType.STUDENT,
        raw_password="first password",
        administrator=administrator,
    )
    deleted_pk = student.pk
    assert register_login_failure(
        actor_hint=account_name,
        client_ip="192.0.2.242",
        secret="test-secret-that-is-never-persisted",
        limit=1,
        window_seconds=60,
    ) is False
    assert not is_login_allowed(
        actor_hint=account_name,
        client_ip="192.0.2.242",
        secret="test-secret-that-is-never-persisted",
    )
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda actor_id: ActorErasureResult(module_counts=(("m0", 1),)),
    )

    account_governance.erase_managed_account(
        user_id=student.pk,
        expected_type=AccountType.STUDENT,
        confirmed_account_name=account_name,
        administrator=administrator,
    )
    replacement = account_governance.create_managed_account(
        account_name=account_name,
        account_type=AccountType.STUDENT,
        raw_password="replacement password",
        administrator=administrator,
    )

    assert replacement.pk != deleted_pk
    assert replacement.username == account_name
    assert is_login_allowed(
        actor_hint=account_name,
        client_ip="192.0.2.243",
        secret="test-secret-that-is-never-persisted",
    )


def test_teacher_deletion_physically_removes_owned_class_and_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    student = _user(
        "pseudonym_student_delete_teacher_class",
        AccountType.STUDENT,
    )
    other_teacher = _user(
        "pseudonym_teacher_delete_survivor",
        AccountType.TEACHER,
    )
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
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
    )
    ActorGrant.objects.create(
        user=student,
        role=RoleName.STUDENT,
        course_id=workspace.course_id,
        class_id=workspace.class_id,
        source_checksum="a" * 64,
    )
    source = CourseSource.objects.create(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
        display_name="知识文件",
        source_type=CourseSource.SourceType.KNOWLEDGE,
        created_by=teacher,
    )
    storage_key = f"{'f' * 32}/{uuid4()}"
    storage_root = tmp_path / "knowledge_uploads"
    storage_path = storage_root / storage_key
    storage_path.parent.mkdir(parents=True)
    storage_path.write_bytes(b"course material")
    source_version = CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=storage_key,
        sha256="b" * 64,
        media_type="text/plain",
        size_bytes=15,
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
        requested_by=teacher,
        change_set_checksum="c" * 64,
    )
    release = CourseKnowledgeRelease.objects.create(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
        version_number=1,
        status=CourseKnowledgeRelease.Status.ACTIVE,
        job=job,
        content_checksum="d" * 64,
    )
    workspace.active_release = release
    workspace.save(update_fields=("active_release",))
    ClassLearningSnapshot.objects.create(
        workspace=workspace,
        release=release,
        input_checksum="e" * 64,
        roster_checksum="f" * 64,
        reason="erasure_fixture",
    )
    surviving_workspace = CourseClassWorkspace.objects.create(
        course_id="course_survives_teacher_delete",
        class_id="class_survives_teacher_delete",
        owner_teacher=other_teacher,
    )
    ClassMembership.objects.create(
        workspace=surviving_workspace,
        student=student,
        added_by_teacher=other_teacher,
    )
    monkeypatch.setattr(
        account_governance,
        "_purge_runtime_actor",
        lambda value: ActorErasureResult(module_counts=()),
    )
    client = Client()
    client.force_login(administrator)

    with override_settings(COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=storage_root):
        response = client.post(
            reverse(
                "account-admin-delete-teacher",
                kwargs={"user_id": teacher.pk},
            ),
            {"confirmed_account_name": teacher.username},
        )

    assert response.status_code == 302
    assert not User.objects.filter(pk=teacher_pk).exists()
    assert User.objects.filter(pk=student.pk).exists()
    assert not CourseClassWorkspace.objects.filter(pk=workspace.pk).exists()
    assert CourseClassWorkspace.objects.filter(pk=surviving_workspace.pk).exists()
    assert not ClassMembership.objects.filter(workspace_id=workspace.pk).exists()
    assert ClassMembership.objects.filter(
        workspace=surviving_workspace,
        student=student,
    ).exists()
    assert not ActorGrant.objects.filter(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    ).exists()
    assert not CourseSource.objects.filter(pk=source.pk).exists()
    assert not CourseSourceVersion.objects.filter(pk=source_version.pk).exists()
    assert not KnowledgeIngestionJob.objects.filter(pk=job.pk).exists()
    assert not CourseKnowledgeRelease.objects.filter(pk=release.pk).exists()
    assert not ClassLearningSnapshot.objects.filter(workspace_id=workspace.pk).exists()
    assert not ScopedDeepSeekConfiguration.objects.filter(
        workspace_id=workspace.pk
    ).exists()
    assert not storage_path.exists()
    assert not ErasureFileCleanup.objects.filter(
        kind=ErasureFileCleanup.Kind.KNOWLEDGE_UPLOAD,
        opaque_key=storage_key,
    ).exists()


def test_student_deletion_physically_removes_own_frozen_submission(
    tmp_path: Path,
) -> None:
    administrator = _user(
        "pseudonym_admin_delete_frozen_submission",
        AccountType.ADMINISTRATOR,
    )
    _permission(administrator, "manage_accounts")
    student = _user(
        "pseudonym_student_delete_frozen_submission",
        AccountType.STUDENT,
    )
    survivor = _user(
        "pseudonym_student_keep_frozen_submission",
        AccountType.STUDENT,
    )
    attempt_id = "attempt_delete_frozen_submission"
    survivor_attempt_id = "attempt_keep_frozen_submission"
    runtime_root = tmp_path / "runtime"
    frozen_root = runtime_root / "frozen_submissions"
    frozen_root.mkdir(parents=True)
    target_file = frozen_root / f"{attempt_id}.json"
    target_file.write_text("{}", encoding="utf-8")
    raw_connection = connection.connection
    assert raw_connection is not None
    raw_connection.execute(
        """
        CREATE TABLE m0_assessment_runs (
            course_id TEXT NOT NULL,
            class_id TEXT NOT NULL,
            learner_id TEXT NOT NULL,
            attempt_id TEXT
        )
        """
    )
    raw_connection.executemany(
        """
        INSERT INTO m0_assessment_runs(
            course_id, class_id, learner_id, attempt_id
        ) VALUES (?, ?, ?, ?)
        """,
        (
            ("course_frozen", "class_frozen", student.actor_id, attempt_id),
            (
                "course_survivor",
                "class_survivor",
                survivor.actor_id,
                survivor_attempt_id,
            ),
        ),
    )
    client = Client()
    client.force_login(administrator)

    try:
        with override_settings(COURSE_INSIGHT_RUNTIME_DIR=runtime_root):
            response = client.post(
                reverse(
                    "account-admin-delete-student",
                    kwargs={"user_id": student.pk},
                ),
                {"confirmed_account_name": student.username},
            )
    finally:
        raw_connection.execute("DROP TABLE m0_assessment_runs")

    assert response.status_code == 302
    assert not User.objects.filter(pk=student.pk).exists()
    assert target_file.exists() is False
    assert not ErasureFileCleanup.objects.filter(
        kind=ErasureFileCleanup.Kind.FROZEN_SUBMISSION,
        opaque_key=attempt_id,
    ).exists()


def test_failed_runtime_erasure_keeps_account_available_for_a_safe_retry(
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
            kwargs={"user_id": student.pk},
        ),
        {"confirmed_account_name": student.username},
    )

    student.refresh_from_db()
    assert response.status_code == 400
    assert "注销未完成" in response.content.decode()
    assert "已冻结" not in response.content.decode()
    assert student.is_active is True
