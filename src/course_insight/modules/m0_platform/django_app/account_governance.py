"""Administrator-owned account creation and physical-erasure services."""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import transaction

from course_insight.modules.m0_platform.django_app.authz import (
    ROLE_PERMISSIONS,
    authorize_account_admin,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountLifecycleEvent,
    AccountType,
    DeletedActorFingerprint,
    RoleName,
    User,
)


_MANAGED_ACCOUNT_TYPES = frozenset({AccountType.STUDENT, AccountType.TEACHER})


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
