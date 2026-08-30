from __future__ import annotations

from importlib import import_module

import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission

from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _user(actor_id: str, account_type: str) -> User:
    return User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        account_type=account_type,
        password="Strong-password-123!",
    )


def test_authorization_backfill_repairs_existing_teacher_accounts() -> None:
    """The additive data migration repairs accounts created before class tools."""

    teacher = _user("pseudonym_teacher_backfill", AccountType.TEACHER)
    administrator = _user(
        "pseudonym_administrator_backfill",
        AccountType.ADMINISTRATOR,
    )
    legacy_teacher_group, _ = Group.objects.get_or_create(name="teacher")
    legacy_teacher_group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="view_class_analytics",
        )
    )
    teacher.groups.add(legacy_teacher_group)

    migration = import_module(
        "course_insight.modules.m0_platform.django_app.migrations.0023_backfill_account_authorization"
    )
    migration.backfill_account_authorization(apps, None)

    teacher.refresh_from_db()
    administrator.refresh_from_db()
    assert teacher.account_type == AccountType.TEACHER
    assert teacher.has_perm("m0_platform_web.open_class")
    assert teacher.has_perm("m0_platform_web.manage_class_members")
    assert not teacher.has_perm("m0_platform_web.manage_accounts")
    assert administrator.has_perm("m0_platform_web.manage_accounts")
