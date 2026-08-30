from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from django.contrib.auth import authenticate
from django.test import override_settings

from course_insight.desktop import bootstrap
from course_insight.desktop.bootstrap import (
    DesktopBootstrapError,
    create_initial_admin,
    initial_admin_required,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ActorGrant,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)


def test_desktop_migration_builds_a_fresh_local_database(
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    script = """
import os
from pathlib import Path
from course_insight.desktop.bootstrap import (
    configure_process_environment,
    migrate_database,
    prepare_installation,
)
from course_insight.desktop.paths import resolve_desktop_paths

paths = resolve_desktop_paths(os.environ, resources_root=Path.cwd())
prepare_installation(paths)
configure_process_environment(paths)
migrate_database()
"""
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(local_app_data)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    database = local_app_data / "KeYeZhiXi" / "runtime" / "course_insight.db"
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        applied = connection.execute(
            "SELECT COUNT(*) FROM django_migrations"
        ).fetchone()
        user_table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'm0_platform_web_user'"
        ).fetchone()
    assert applied is not None and applied[0] > 0
    assert user_table == ("m0_platform_web_user",)


def _roles_path(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "roles.csv"
    path.write_text(
        "actor_id,role,course_id,class_id,is_active\n",
        encoding="utf-8",
    )
    return path


def test_first_administrator_reuses_the_canonical_role_and_can_log_in(
    tmp_path: Path,
) -> None:
    roles_path = _roles_path(tmp_path)
    account_name = " 本地管理员 "
    password = " 任意 本地 密码 "

    with override_settings(
        COURSE_INSIGHT_CONFIG_DIR=roles_path.parent,
        COURSE_INSIGHT_ROLES_PATH=roles_path,
    ):
        assert initial_admin_required() is True
        administrator = create_initial_admin(account_name, password)

    administrator.refresh_from_db()
    assert administrator.username == account_name
    assert administrator.account_type == AccountType.ADMINISTRATOR
    assert administrator.actor_id.startswith("pseudonym_")
    assert administrator.check_password(password)
    assert administrator.groups.filter(name="system_admin").exists()
    assert administrator.has_perm("m0_platform_web.manage_accounts")
    assert ActorGrant.objects.filter(
        user=administrator,
        role="system_admin",
        course_id__isnull=True,
        class_id__isnull=True,
        is_active=True,
    ).exists()
    assert authenticate(username=account_name, password=password) == administrator
    assert initial_admin_required() is False
    role_seed = roles_path.read_text(encoding="utf-8")
    assert administrator.actor_id in role_seed
    assert account_name not in role_seed
    assert password not in role_seed


@pytest.mark.parametrize(
    ("account_name", "password"),
    [("", "password"), ("   ", "password"), ("administrator", ""), ("administrator", " \t ")],
)
def test_first_administrator_rejects_blank_credentials_without_side_effects(
    tmp_path: Path,
    account_name: str,
    password: str,
) -> None:
    roles_path = _roles_path(tmp_path)

    with override_settings(
        COURSE_INSIGHT_CONFIG_DIR=roles_path.parent,
        COURSE_INSIGHT_ROLES_PATH=roles_path,
    ), pytest.raises(DesktopBootstrapError, match="不能为空"):
        create_initial_admin(account_name, password)

    assert User.objects.count() == 0
    assert roles_path.read_text(encoding="utf-8") == (
        "actor_id,role,course_id,class_id,is_active\n"
    )


def test_first_administrator_cannot_run_after_any_account_exists(
    tmp_path: Path,
) -> None:
    roles_path = _roles_path(tmp_path)
    User.objects.create_user(
        username="existing-account",
        actor_id="pseudonym_existing_account",
        password="existing-password",
    )

    with override_settings(
        COURSE_INSIGHT_CONFIG_DIR=roles_path.parent,
        COURSE_INSIGHT_ROLES_PATH=roles_path,
    ), pytest.raises(DesktopBootstrapError, match="系统已经存在账户"):
        create_initial_admin("new-administrator", "new-password")

    assert User.objects.count() == 1


def test_first_administrator_rolls_back_a_partial_role_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roles_path = _roles_path(tmp_path)

    def failing_role_sync(*args: object, **kwargs: object) -> None:
        del args, kwargs
        User.objects.create(
            username="partial-admin",
            actor_id="pseudonym_partial_admin",
            account_type=AccountType.ADMINISTRATOR,
        )
        raise RuntimeError("simulated role synchronization failure")

    monkeypatch.setattr(bootstrap, "call_command", failing_role_sync)

    with override_settings(
        COURSE_INSIGHT_CONFIG_DIR=roles_path.parent,
        COURSE_INSIGHT_ROLES_PATH=roles_path,
    ), pytest.raises(
        DesktopBootstrapError,
        match="首个管理员创建失败",
    ):
        create_initial_admin("administrator", "password")

    assert User.objects.count() == 0
