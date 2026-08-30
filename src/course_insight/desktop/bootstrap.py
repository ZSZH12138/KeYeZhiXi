"""Prepare one local installation and create its one-time administrator."""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import MutableMapping
from io import StringIO
from pathlib import Path

from django.core.management import call_command

from course_insight.desktop.paths import DesktopPaths, desktop_environment


_ROLE_HEADER = "actor_id,role,course_id,class_id,is_active\n"
_CONFIG_TEMPLATES = {
    "app.example.json": "app.json",
    "state.json": "state.json",
    "teacher.json": "teacher.json",
}
_DIRECT_CONFIGURATION_KEYS = frozenset(
    {
        "CONFIG_DIR",
        "DATABASE_PATH",
        "DATABASE_URL",
        "DJANGO_ALLOWED_HOSTS",
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "DJANGO_SECRET_KEY",
        "ENVIRONMENT",
        "LOG_LEVEL",
        "RUNTIME_DIR",
    }
)


class DesktopBootstrapError(RuntimeError):
    """Raised when a desktop installation cannot be initialized safely."""


def prepare_installation(paths: DesktopPaths) -> None:
    """Create safe local defaults without replacing user-owned values."""

    source_config = paths.resources_root / "config"
    sources = {
        source_config / source_name: paths.config_dir / destination_name
        for source_name, destination_name in _CONFIG_TEMPLATES.items()
    }
    if any(not source.is_file() for source in sources):
        raise DesktopBootstrapError("桌面版配置模板不完整")

    try:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        for source, destination in sources.items():
            _write_new_bytes(destination, source.read_bytes())
        _write_new_text(paths.config_dir / "roles.csv", _ROLE_HEADER)
        _write_new_text(
            paths.data_root / ".env",
            f"DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}\n",
        )
    except DesktopBootstrapError:
        raise
    except OSError as error:
        raise DesktopBootstrapError("本地应用数据目录不可写") from error


def configure_process_environment(
    paths: DesktopPaths,
    environment: MutableMapping[str, str] | None = None,
) -> MutableMapping[str, str]:
    """Remove inherited config overrides and select the desktop data root."""

    target = os.environ if environment is None else environment
    for key in tuple(target):
        if (
            key.startswith("COURSE_INSIGHT_")
            or key.startswith("DJANGO_COURSE_INSIGHT_")
            or key in _DIRECT_CONFIGURATION_KEYS
        ):
            target.pop(key, None)
    target.update(desktop_environment(paths))
    return target


def migrate_database() -> None:
    """Apply all Django migrations before any service process starts."""

    _ensure_django_ready()
    call_command("migrate", interactive=False, verbosity=0)


def initial_admin_required() -> bool:
    """Return true only for a completely empty account database."""

    _ensure_django_ready()
    from course_insight.modules.m0_platform.django_app.models import User

    return not User.objects.exists()


def create_initial_admin(account_name: str, raw_password: str):
    """Atomically create the sole recovery-seeded system administrator."""

    _require_nonblank(account_name, field="账户名")
    _require_nonblank(raw_password, field="密码")
    _ensure_django_ready()

    from django.conf import settings
    from django.db import transaction

    from course_insight.modules.m0_platform.django_app.models import (
        AccountType,
        User,
    )

    if User.objects.exists():
        raise DesktopBootstrapError("系统已经存在账户，首次初始化已关闭")

    actor_id = f"pseudonym_{uuid.uuid4().hex}"
    roles_path = _safe_roles_path(
        Path(settings.COURSE_INSIGHT_CONFIG_DIR),
        Path(settings.COURSE_INSIGHT_ROLES_PATH),
    )
    try:
        with transaction.atomic():
            if User.objects.select_for_update().exists():
                raise DesktopBootstrapError(
                    "系统已经存在账户，首次初始化已关闭"
                )
            _replace_text(
                roles_path,
                _ROLE_HEADER + f"{actor_id},system_admin,,,true\n",
            )
            call_command(
                "sync_roles",
                "--apply",
                verbosity=0,
                stdout=StringIO(),
                stderr=StringIO(),
            )
            user = User.objects.select_for_update().get(actor_id=actor_id)
            user.username = account_name
            user.account_type = AccountType.ADMINISTRATOR
            user.set_password(raw_password)
            user.full_clean()
            user.save()
    except DesktopBootstrapError:
        raise
    except Exception as error:
        raise DesktopBootstrapError("首个管理员创建失败") from error
    return user


def _ensure_django_ready() -> None:
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


def _require_nonblank(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopBootstrapError(f"{field}不能为空或全为空白")
    return value


def _safe_roles_path(config_root: Path, roles_path: Path) -> Path:
    root = config_root.resolve()
    candidate = roles_path.resolve()
    if candidate.parent != root:
        raise DesktopBootstrapError("角色配置路径不安全")
    return candidate


def _write_new_bytes(path: Path, content: bytes) -> None:
    if path.exists():
        return
    _atomic_write(path, content)


def _write_new_text(path: Path, content: str) -> None:
    if path.exists():
        return
    _atomic_write(path, content.encode("utf-8"))


def _replace_text(path: Path, content: str) -> None:
    _atomic_write(path, content.encode("utf-8"))


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
