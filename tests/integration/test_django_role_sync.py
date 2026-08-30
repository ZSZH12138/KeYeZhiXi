from __future__ import annotations

import io
import os
from pathlib import Path

import django
import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    AccountType,
    ActorGrant,
    RoleSyncState,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)
HEADER = "actor_id,role,course_id,class_id,is_active\n"


def _write_roles(path: Path, *rows: str) -> None:
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


def _run_sync(
    config_dir: Path,
    mode: str,
) -> str:
    output = io.StringIO()
    with override_settings(
        COURSE_INSIGHT_CONFIG_DIR=config_dir,
        COURSE_INSIGHT_ROLES_PATH=config_dir / "roles.csv",
    ):
        call_command(
            "sync_roles",
            **{mode.replace("-", "_"): True},
            stdout=output,
        )
    return output.getvalue()


def test_check_and_dry_run_validate_without_mutating_database(
    tmp_path: Path,
) -> None:
    roles_path = tmp_path / "roles.csv"
    _write_roles(
        roles_path,
        "pseudonym_student_001,student,course_a,class_1,true",
    )

    check_output = _run_sync(tmp_path, "check")
    dry_run_output = _run_sync(tmp_path, "dry-run")

    assert User.objects.count() == 0
    assert ActorGrant.objects.count() == 0
    assert RoleSyncState.objects.count() == 0
    assert "pseudonym_student_001" not in check_output + dry_run_output
    assert str(tmp_path) not in check_output + dry_run_output


def test_apply_bootstraps_only_account_administrator(
    tmp_path: Path,
) -> None:
    roles_path = tmp_path / "roles.csv"
    _write_roles(
        roles_path,
        "pseudonym_student_001,student,course_a,class_1,true",
        "pseudonym_teacher_001,teacher,course_a,class_1,true",
        "pseudonym_course_admin_001,course_admin,course_a,,true",
        "pseudonym_system_admin_001,system_admin,,,true",
    )

    output = _run_sync(tmp_path, "apply")

    assert User.objects.count() == 1
    assert ActorGrant.objects.count() == 1
    assert set(Group.objects.values_list("name", flat=True)) >= {
        "student",
        "teacher",
        "course_admin",
        "system_admin",
    }
    administrator = User.objects.get(actor_id="pseudonym_system_admin_001")
    assert not administrator.has_usable_password()
    assert administrator.email == ""
    assert administrator.account_type == AccountType.ADMINISTRATOR
    assert administrator.groups.filter(name="system_admin").exists()
    assert administrator.has_perm("m0_platform_web.manage_accounts")
    assert not administrator.has_perm("m0_platform_web.view_class_analytics")
    assert not administrator.has_perm("m0_platform_web.start_assessment")
    assert not administrator.has_perm("m0_platform_web.open_class")
    assert not administrator.has_perm("m0_platform_web.manage_class_members")
    teacher_permissions = set(
        Group.objects.get(name="teacher").permissions.values_list(
            "codename",
            flat=True,
        )
    )
    assert {"open_class", "manage_class_members"} <= teacher_permissions
    state = RoleSyncState.objects.get(pk="roles")
    assert len(state.source_checksum) == 64
    assert state.grant_count == 1
    assert "pseudonym_" not in output
    assert str(tmp_path) not in output


def test_repeated_apply_is_idempotent(tmp_path: Path) -> None:
    roles_path = tmp_path / "roles.csv"
    _write_roles(
        roles_path,
        "pseudonym_system_admin_001,system_admin,,,true",
    )

    _run_sync(tmp_path, "apply")
    first_state = RoleSyncState.objects.get(pk="roles")
    first_grant = ActorGrant.objects.get()
    first_user_id = first_grant.user_id
    _run_sync(tmp_path, "apply")
    second_state = RoleSyncState.objects.get(pk="roles")

    assert User.objects.count() == 1
    assert ActorGrant.objects.count() == 1
    assert ActorGrant.objects.get().user_id == first_user_id
    assert second_state.source_checksum == first_state.source_checksum
    assert second_state.synchronized_at == first_state.synchronized_at


def test_explicit_inactive_seed_revokes_and_can_reactivate_grant(
    tmp_path: Path,
) -> None:
    roles_path = tmp_path / "roles.csv"
    active = "pseudonym_system_admin_001,system_admin,,,true"
    inactive = "pseudonym_system_admin_001,system_admin,,,false"
    _write_roles(roles_path, active)
    _run_sync(tmp_path, "apply")

    _write_roles(roles_path, inactive)
    _run_sync(tmp_path, "apply")
    revoked = ActorGrant.objects.get()
    user = revoked.user
    assert revoked.is_active is False
    assert revoked.revoked_at is not None
    assert not user.groups.filter(name="system_admin").exists()
    first_revoked_at = revoked.revoked_at
    first_state_time = RoleSyncState.objects.get(pk="roles").synchronized_at

    _run_sync(tmp_path, "apply")
    repeated = ActorGrant.objects.get()
    assert repeated.revoked_at == first_revoked_at
    assert not repeated.user.groups.filter(name="system_admin").exists()
    assert RoleSyncState.objects.get(pk="roles").synchronized_at == (
        first_state_time
    )

    _write_roles(roles_path, active)
    _run_sync(tmp_path, "apply")
    reactivated = ActorGrant.objects.get()
    assert reactivated.is_active is True
    assert reactivated.revoked_at is None
    assert reactivated.user.groups.filter(name="system_admin").exists()


def test_omitted_grant_is_revoked_by_apply_and_reported_by_dry_run(
    tmp_path: Path,
) -> None:
    roles_path = tmp_path / "roles.csv"
    administrator = "pseudonym_system_admin_001,system_admin,,,true"
    _write_roles(roles_path, administrator)
    _run_sync(tmp_path, "apply")

    _write_roles(roles_path)
    dry_run_output = _run_sync(tmp_path, "dry-run")

    administrator_grant = ActorGrant.objects.get(
        user__actor_id="pseudonym_system_admin_001"
    )
    assert administrator_grant.is_active is True
    assert administrator_grant.user.groups.filter(name="system_admin").exists()
    assert "revoke=1" in dry_run_output

    _run_sync(tmp_path, "apply")

    administrator_grant.refresh_from_db()
    assert administrator_grant.is_active is False
    assert administrator_grant.revoked_at is not None
    assert not administrator_grant.user.groups.filter(name="system_admin").exists()


def test_invalid_seed_fails_before_writes_and_preserves_previous_state(
    tmp_path: Path,
) -> None:
    roles_path = tmp_path / "roles.csv"
    _write_roles(
        roles_path,
        "pseudonym_system_admin_001,system_admin,,,true",
    )
    _run_sync(tmp_path, "apply")
    previous_state = RoleSyncState.objects.get(pk="roles")
    previous_grant = ActorGrant.objects.get()

    _write_roles(
        roles_path,
        "pseudonym_actor_001,student,course_a,class_1,true",
        "pseudonym_actor_001,teacher,course_a,class_1,true",
    )
    with pytest.raises(CommandError):
        _run_sync(tmp_path, "apply")

    assert ActorGrant.objects.count() == 1
    assert ActorGrant.objects.get().pk == previous_grant.pk
    assert RoleSyncState.objects.get(pk="roles").source_checksum == (
        previous_state.source_checksum
    )


def test_apply_rolls_back_when_canonical_permissions_are_unavailable(
    tmp_path: Path,
) -> None:
    Permission.objects.filter(
        content_type__app_label="m0_platform_web",
        codename="review_score",
    ).delete()
    roles_path = tmp_path / "roles.csv"
    _write_roles(
        roles_path,
        "pseudonym_teacher_001,teacher,course_a,class_1,true",
    )

    with pytest.raises(CommandError, match="permissions are unavailable"):
        _run_sync(tmp_path, "apply")

    assert User.objects.count() == 0
    assert ActorGrant.objects.count() == 0
    assert RoleSyncState.objects.count() == 0
