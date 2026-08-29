"""Administrator-owned account creation and physical-erasure services."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F

from course_insight.application.actor_erasure import (
    ActorErasureCoordinator,
    ActorErasureResult,
)
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.authz import (
    ROLE_PERMISSIONS,
    authorize_account_admin,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountLifecycleEvent,
    AccountType,
    AuthenticatedSession,
    ClassMembership,
    CourseClassWorkspace,
    DeletedActorFingerprint,
    RoleName,
    ScopedDeepSeekConfiguration,
    User,
)


_MANAGED_ACCOUNT_TYPES = frozenset({AccountType.STUDENT, AccountType.TEACHER})


@dataclass(frozen=True, slots=True)
class AccountErasurePreview:
    actor_id: str
    account_type: str
    active_membership_count: int
    owned_workspace_count: int


@dataclass(frozen=True, slots=True)
class ManagedAccountErasureResult:
    runtime: ActorErasureResult
    django_deleted_count: int


def actor_fingerprint(actor_id: str) -> str:
    """Return a domain-separated one-way marker for a managed actor ID."""

    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        b"course-insight-deleted-actor\0" + actor_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@transaction.atomic
def create_managed_account(
    *,
    actor_id: str,
    account_type: str,
    raw_password: str,
    administrator: User,
) -> User:
    """Create one teacher/student account through the administrator boundary."""

    authorize_account_admin(administrator)
    if account_type not in _MANAGED_ACCOUNT_TYPES:
        raise ValidationError("只能创建教师或学生账户")
    digest = actor_fingerprint(actor_id)
    if DeletedActorFingerprint.objects.filter(target_digest=digest).exists():
        raise ValidationError("该账户名已被注销，不能重复使用")
    if User.objects.filter(actor_id=actor_id).exists():
        raise ValidationError("该账户名已存在")

    user = User(
        username=actor_id,
        actor_id=actor_id,
        account_type=account_type,
    )
    user.set_password(raw_password)
    user.full_clean()
    user.save()
    _assign_managed_permissions(user)
    AccountLifecycleEvent.objects.create(
        action=AccountLifecycleEvent.Action.CREATED,
        target_digest=digest,
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
    codenames = set(ROLE_PERMISSIONS[role])
    if role == RoleName.TEACHER:
        codenames.update({"open_class", "manage_class_members"})
    group, _ = Group.objects.get_or_create(name=role)
    permissions = Permission.objects.filter(
        content_type__app_label="m0_platform_web",
        codename__in=codenames,
    )
    group.permissions.add(*permissions)
    user.groups.add(group)


def preview_account_erasure(
    *,
    actor_id: str,
    expected_type: str,
) -> AccountErasurePreview:
    target = _managed_target(actor_id, expected_type)
    return AccountErasurePreview(
        actor_id=target.actor_id,
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
    actor_id: str,
    expected_type: str,
    confirmed_actor_id: str,
    administrator: User,
) -> ManagedAccountErasureResult:
    """Revoke access, purge module data, then physically delete the account."""

    authorize_account_admin(administrator)
    if confirmed_actor_id != actor_id:
        raise ValidationError("确认账户名不一致")
    target = _managed_target(actor_id, expected_type)
    digest = actor_fingerprint(actor_id)
    affected_workspace_ids = tuple(
        ClassMembership.objects.filter(
            student=target,
            status=ClassMembership.Status.ACTIVE,
        ).values_list("workspace_id", flat=True)
    )
    owned_workspace_ids = tuple(
        CourseClassWorkspace.objects.filter(owner_teacher=target).values_list(
            "pk",
            flat=True,
        )
    )
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=target.pk)
        locked.is_active = False
        locked.save(update_fields=("is_active",))
        AccountLifecycleEvent.objects.create(
            action=AccountLifecycleEvent.Action.DELETION_STARTED,
            target_digest=digest,
            target_account_type=expected_type,
            administrator=administrator,
        )

    try:
        runtime_result = _purge_runtime_actor(actor_id)
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=target.pk)
            session_keys = _session_keys_for_user(locked)
            if session_keys:
                Session.objects.filter(session_key__in=session_keys).delete()
            if expected_type == AccountType.TEACHER:
                ScopedDeepSeekConfiguration.objects.filter(
                    workspace_id__in=owned_workspace_ids
                ).delete()
                CourseClassWorkspace.objects.filter(owner_teacher=locked).update(
                    owner_teacher=None,
                    status=CourseClassWorkspace.Status.ARCHIVED,
                )
            else:
                CourseClassWorkspace.objects.filter(
                    pk__in=affected_workspace_ids
                ).update(roster_version=F("roster_version") + 1)
            DeletedActorFingerprint.objects.update_or_create(
                target_digest=digest,
            )
            AccountLifecycleEvent.objects.create(
                action=AccountLifecycleEvent.Action.DELETED,
                target_digest=digest,
                target_account_type=expected_type,
                administrator=administrator,
            )
            django_deleted_count, _ = locked.delete()
            transaction.on_commit(
                lambda: _refresh_affected_rosters(affected_workspace_ids),
                robust=True,
            )
    except Exception:
        AccountLifecycleEvent.objects.create(
            action=AccountLifecycleEvent.Action.DELETE_FAILED,
            target_digest=digest,
            target_account_type=expected_type,
            administrator=administrator,
            success=False,
        )
        raise
    if User.objects.filter(pk=target.pk).exists():
        raise RuntimeError("physical account deletion did not complete")
    return ManagedAccountErasureResult(
        runtime=runtime_result,
        django_deleted_count=django_deleted_count,
    )


def _managed_target(actor_id: str, expected_type: str) -> User:
    if expected_type not in _MANAGED_ACCOUNT_TYPES:
        raise ValidationError("只能注销教师或学生账户")
    target = User.objects.filter(actor_id=actor_id).first()
    if target is None:
        raise ValidationError("账户不存在")
    if target.account_type != expected_type:
        raise ValidationError("账户类型不匹配")
    return target


def _purge_runtime_actor(actor_id: str) -> ActorErasureResult:
    container = runtime.get_application_container()
    return ActorErasureCoordinator.from_container(container).purge(actor_id)


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
