"""Administrator-owned account creation and physical-erasure services."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import F

from course_insight.application.actor_erasure import (
    ActorErasureCoordinator,
    ActorErasureResult,
)
from course_insight.infrastructure.sqlite.actor_erasure import (
    purge_sqlite_actor_connection,
)
from course_insight.infrastructure.sqlite.class_erasure import (
    ClassErasureResult,
    purge_sqlite_class_scope,
)
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.account_file_cleanup import (
    purge_pending_erasure_files,
    queue_erasure_file_cleanup,
)
from course_insight.modules.m0_platform.django_app.authz import (
    ROLE_PERMISSIONS,
    authorize_account_admin,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountLifecycleEvent,
    AccountType,
    AuthenticatedSession,
    ActorGrant,
    ClassMembership,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeIngestionJob,
    RoleName,
    User,
    account_name_digest,
)


_MANAGED_ACCOUNT_TYPES = frozenset({AccountType.STUDENT, AccountType.TEACHER})


@dataclass(frozen=True, slots=True)
class AccountErasurePreview:
    account_name: str
    account_type: str
    active_membership_count: int
    owned_workspace_count: int


@dataclass(frozen=True, slots=True)
class ManagedAccountErasureResult:
    runtime: ActorErasureResult
    django_deleted_count: int


@transaction.atomic
def create_managed_account(
    *,
    account_name: str,
    account_type: str,
    raw_password: str,
    administrator: User,
) -> User:
    """Create one teacher/student account through the administrator boundary."""

    authorize_account_admin(administrator)
    if account_type not in _MANAGED_ACCOUNT_TYPES:
        raise ValidationError("只能创建教师或学生账户")
    _require_nonblank(account_name, field_name="账户名")
    _require_nonblank(raw_password, field_name="密码")
    if User.objects.filter(username_digest=account_name_digest(account_name)).exists():
        raise ValidationError("该账户名已存在")

    user = User(
        username=account_name,
        actor_id=_new_actor_id(),
        account_type=account_type,
    )
    user.set_password(raw_password)
    user.full_clean()
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as error:
        raise ValidationError("该账户名已存在") from error
    _assign_managed_permissions(user)
    AccountLifecycleEvent.objects.create(
        action=AccountLifecycleEvent.Action.CREATED,
        target_user=user,
        target_account_type=account_type,
        administrator=administrator,
        success=True,
    )
    return user


def _assign_managed_permissions(user: User) -> None:
    role = (
        RoleName.TEACHER
        if user.account_type == AccountType.TEACHER
        else RoleName.STUDENT
    )
    group, _ = Group.objects.get_or_create(name=role)
    permissions = Permission.objects.filter(
        content_type__app_label="m0_platform_web",
        codename__in=ROLE_PERMISSIONS[role],
    )
    group.permissions.add(*permissions)
    user.groups.add(group)


def preview_account_erasure(
    *,
    user_id: int,
    expected_type: str,
) -> AccountErasurePreview:
    target = _managed_target(user_id, expected_type)
    return AccountErasurePreview(
        account_name=target.username,
        account_type=target.account_type,
        active_membership_count=ClassMembership.objects.filter(
            student=target,
            status=ClassMembership.Status.ACTIVE,
        ).count(),
        owned_workspace_count=CourseClassWorkspace.objects.filter(
            owner_teacher=target,
            status=CourseClassWorkspace.Status.ACTIVE,
        ).count(),
    )


def erase_managed_account(
    *,
    user_id: int,
    expected_type: str,
    confirmed_account_name: str,
    administrator: User,
) -> ManagedAccountErasureResult:
    """Physically erase one managed account after all scoped cleanup succeeds."""

    authorize_account_admin(administrator)
    target = _managed_target(user_id, expected_type)
    if confirmed_account_name != target.username:
        raise ValidationError("确认账户名不一致")
    try:
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=target.pk)
            if locked.account_type != expected_type:
                raise ValidationError("账户类型不匹配")
            if locked.username != confirmed_account_name:
                raise ValidationError("确认账户名不一致")
            affected_workspace_ids = tuple(
                ClassMembership.objects.filter(
                    student=locked,
                    status=ClassMembership.Status.ACTIVE,
                ).values_list("workspace_id", flat=True)
            )
            owned_workspaces = tuple(
                CourseClassWorkspace.objects.select_for_update()
                .filter(owner_teacher=locked)
                .order_by("workspace_id")
            )
            AccountLifecycleEvent.objects.create(
                action=AccountLifecycleEvent.Action.DELETION_STARTED,
                target_user=locked,
                target_account_type=expected_type,
                administrator=administrator,
            )
            frozen_attempt_ids: set[str] = set()
            storage_keys: set[str] = set()
            frozen_attempt_ids.update(
                _frozen_attempt_ids_for_actor(locked.actor_id)
            )
            if expected_type == AccountType.TEACHER:
                for workspace in owned_workspaces:
                    runtime_scope = _purge_runtime_class_scope(
                        course_id=workspace.course_id,
                        class_id=workspace.class_id,
                    )
                    frozen_attempt_ids.update(runtime_scope.frozen_attempt_ids)
                    storage_keys.update(
                        CourseSourceVersion.objects.filter(
                            source__course_id=workspace.course_id,
                            source__class_id=workspace.class_id,
                        ).values_list("storage_key", flat=True)
                    )
                for workspace in owned_workspaces:
                    _erase_teacher_workspace(workspace)
            queue_erasure_file_cleanup(
                storage_keys=storage_keys,
                frozen_attempt_ids=frozen_attempt_ids,
            )

            runtime_result = _purge_runtime_actor(locked.actor_id)
            session_keys = _session_keys_for_user(locked)
            if session_keys:
                Session.objects.filter(session_key__in=session_keys).delete()
            if expected_type == AccountType.STUDENT:
                CourseClassWorkspace.objects.filter(
                    pk__in=affected_workspace_ids
                ).update(roster_version=F("roster_version") + 1)
            django_deleted_count, _ = locked.delete()
            transaction.on_commit(
                lambda: _refresh_affected_rosters(affected_workspace_ids),
                robust=True,
            )
            transaction.on_commit(purge_pending_erasure_files, robust=True)
    except Exception:
        _record_failed_account_erasure(
            target=target,
            expected_type=expected_type,
            administrator=administrator,
        )
        raise
    if User.objects.filter(pk=target.pk).exists():
        raise RuntimeError("physical account deletion did not complete")
    return ManagedAccountErasureResult(
        runtime=runtime_result,
        django_deleted_count=django_deleted_count,
    )


def _managed_target(user_id: int, expected_type: str) -> User:
    if expected_type not in _MANAGED_ACCOUNT_TYPES:
        raise ValidationError("只能注销教师或学生账户")
    target = User.objects.filter(pk=user_id).first()
    if target is None:
        raise ValidationError("账户不存在")
    if target.account_type != expected_type:
        raise ValidationError("账户类型不匹配")
    return target


def _new_actor_id() -> str:
    """Generate a business-safe internal identifier for all module contracts."""

    return f"pseudonym_{uuid.uuid4().hex}"


def _require_nonblank(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name}不能为空或全为空白")
    return value


def _purge_runtime_actor(actor_id: str) -> ActorErasureResult:
    if connection.vendor == "sqlite":
        connection.ensure_connection()
        raw_connection = connection.connection
        if raw_connection is None:
            raise RuntimeError("SQLite account erasure connection is unavailable")
        return ActorErasureResult(
            module_counts=tuple(
                (
                    module,
                    purge_sqlite_actor_connection(
                        raw_connection,
                        module=module,
                        actor_id=actor_id,
                    ),
                )
                for module in ("m9", "m8", "m7", "m6", "m5", "m4", "m0")
            )
        )
    container = runtime.get_application_container()
    return ActorErasureCoordinator.from_container(container).purge(actor_id)


def _purge_runtime_class_scope(
    *,
    course_id: str,
    class_id: str,
) -> ClassErasureResult:
    if connection.vendor != "sqlite":
        raise RuntimeError(
            "teacher account erasure requires a scoped runtime erasure adapter"
        )
    connection.ensure_connection()
    raw_connection = connection.connection
    if raw_connection is None:
        raise RuntimeError("SQLite class erasure connection is unavailable")
    return purge_sqlite_class_scope(
        raw_connection,
        course_id=course_id,
        class_id=class_id,
    )


def _record_failed_account_erasure(
    *,
    target: User,
    expected_type: str,
    administrator: User,
) -> None:
    """Retain failure evidence only while the target account remains live."""

    try:
        if User.objects.filter(pk=target.pk).exists():
            AccountLifecycleEvent.objects.create(
                action=AccountLifecycleEvent.Action.DELETE_FAILED,
                target_user=target,
                target_account_type=expected_type,
                administrator=administrator,
                success=False,
            )
    except Exception:
        # The original deletion failure is authoritative. Avoid masking it with
        # a best-effort audit write that may face the same database outage.
        return None


def _frozen_attempt_ids_for_actor(actor_id: str) -> tuple[str, ...]:
    """Return only the actor's persisted frozen-submission identifiers."""

    if connection.vendor != "sqlite":
        return ()
    connection.ensure_connection()
    raw_connection = connection.connection
    if raw_connection is None:
        raise RuntimeError("SQLite frozen-submission connection is unavailable")
    table = raw_connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'm0_assessment_runs'
        """
    ).fetchone()
    if table is None:
        return ()
    columns = {
        str(row[1])
        for row in raw_connection.execute(
            "PRAGMA table_info('m0_assessment_runs')"
        ).fetchall()
    }
    if {"learner_id", "attempt_id"} - columns:
        return ()
    rows = raw_connection.execute(
        """
        SELECT DISTINCT attempt_id
        FROM m0_assessment_runs
        WHERE learner_id = ? AND attempt_id IS NOT NULL
        """,
        (actor_id,),
    ).fetchall()
    return tuple(sorted(str(row[0]) for row in rows if row[0] is not None))


def _erase_teacher_workspace(workspace: CourseClassWorkspace) -> None:
    """Delete one teacher-owned container and every database object it owns."""

    scope = {
        "course_id": workspace.course_id,
        "class_id": workspace.class_id,
    }
    CourseClassWorkspace.objects.filter(pk=workspace.pk).update(active_release=None)
    CourseKnowledgeRelease.objects.filter(**scope).delete()
    KnowledgeIngestionJob.objects.filter(**scope).delete()
    CourseSourceVersion.objects.filter(
        source__course_id=workspace.course_id,
        source__class_id=workspace.class_id,
    ).delete()
    CourseSource.objects.filter(**scope).delete()
    ActorGrant.objects.filter(**scope).delete()
    CourseClassWorkspace.objects.filter(pk=workspace.pk).delete()


def _session_keys_for_user(user: User) -> tuple[str, ...]:
    """Find indexed and pre-index sessions so no login residue survives."""

    keys = set(
        AuthenticatedSession.objects.filter(user=user).values_list(
            "session_key",
            flat=True,
        )
    )
    user_id = str(user.pk)
    for session in Session.objects.all().iterator(chunk_size=500):
        try:
            decoded = session.get_decoded()
        except (TypeError, ValueError):
            continue
        if str(decoded.get("_auth_user_id", "")) == user_id:
            keys.add(session.session_key)
    return tuple(sorted(keys))


def _refresh_affected_rosters(workspace_ids: tuple[object, ...]) -> None:
    from course_insight.modules.m5_learner_class_state.class_snapshot import (
        synchronize_class_learning_snapshot,
    )

    for workspace in CourseClassWorkspace.objects.filter(
        pk__in=workspace_ids,
        status=CourseClassWorkspace.Status.ACTIVE,
    ):
        synchronize_class_learning_snapshot(
            workspace=workspace,
            reason="account_deleted",
        )
