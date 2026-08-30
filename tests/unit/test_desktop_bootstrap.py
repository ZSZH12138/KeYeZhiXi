from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.desktop.bootstrap import (
    DesktopBootstrapError,
    configure_process_environment,
    prepare_installation,
)
from course_insight.desktop.paths import DesktopPaths


def _paths(tmp_path: Path) -> DesktopPaths:
    return DesktopPaths(
        data_root=tmp_path / "local" / "KeYeZhiXi",
        config_dir=tmp_path / "local" / "KeYeZhiXi" / "config",
        runtime_dir=tmp_path / "local" / "KeYeZhiXi" / "runtime",
        logs_dir=tmp_path / "local" / "KeYeZhiXi" / "logs",
        resources_root=tmp_path / "bundle",
    )


def _write_resources(paths: DesktopPaths) -> None:
    config = paths.resources_root / "config"
    config.mkdir(parents=True)
    (config / "app.example.json").write_text(
        '{"environment":"development"}',
        encoding="utf-8",
    )
    (config / "state.json").write_text(
        '{"aggregation_policy_version":"1.0.0"}',
        encoding="utf-8",
    )
    (config / "teacher.json").write_text(
        '{"minimum_coverage":1.0}',
        encoding="utf-8",
    )


def test_prepare_installation_creates_only_safe_mutable_defaults(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_resources(paths)

    prepare_installation(paths)

    assert paths.runtime_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert (paths.config_dir / "app.json").read_text(encoding="utf-8") == (
        '{"environment":"development"}'
    )
    assert (paths.config_dir / "state.json").is_file()
    assert (paths.config_dir / "teacher.json").is_file()
    assert (paths.config_dir / "roles.csv").read_text(encoding="utf-8") == (
        "actor_id,role,course_id,class_id,is_active\n"
    )
    dotenv = (paths.data_root / ".env").read_text(encoding="utf-8")
    assert dotenv.startswith("DJANGO_SECRET_KEY=")
    assert len(dotenv.partition("=")[2].strip()) >= 64
    assert "password" not in dotenv.lower()
    assert not tuple(paths.data_root.rglob("*.tmp"))


def test_prepare_installation_is_idempotent_and_preserves_local_values(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_resources(paths)
    prepare_installation(paths)
    (paths.config_dir / "app.json").write_text(
        '{"environment":"custom"}',
        encoding="utf-8",
    )
    original_secret = (paths.data_root / ".env").read_text(encoding="utf-8")

    prepare_installation(paths)

    assert (paths.config_dir / "app.json").read_text(encoding="utf-8") == (
        '{"environment":"custom"}'
    )
    assert (paths.data_root / ".env").read_text(
        encoding="utf-8"
    ) == original_secret


def test_prepare_installation_fails_safely_when_a_template_is_missing(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_resources(paths)
    (paths.resources_root / "config" / "teacher.json").unlink()

    with pytest.raises(
        DesktopBootstrapError,
        match="桌面版配置模板不完整",
    ):
        prepare_installation(paths)


def test_configure_process_environment_removes_external_config_overrides(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    environment = {
        "LOCALAPPDATA": str(tmp_path),
        "PATH": "safe-path",
        "COURSE_INSIGHT_RUNTIME_DIR": "C:/outside/runtime",
        "COURSE_INSIGHT_DATABASE__SQLITE_PATH": "C:/outside/data.db",
        "RUNTIME_DIR": "C:/legacy/runtime",
        "DATABASE_URL": "postgresql://secret@example.invalid/database",
        "DJANGO_SECRET_KEY": "external-secret",
        "DJANGO_COURSE_INSIGHT_PROJECT_ROOT": "C:/stale",
    }

    configured = configure_process_environment(paths, environment)

    assert configured is environment
    assert configured["PATH"] == "safe-path"
    assert "COURSE_INSIGHT_RUNTIME_DIR" not in configured
    assert "COURSE_INSIGHT_DATABASE__SQLITE_PATH" not in configured
    assert "RUNTIME_DIR" not in configured
    assert "DATABASE_URL" not in configured
    assert "DJANGO_SECRET_KEY" not in configured
    assert configured["DJANGO_COURSE_INSIGHT_PROJECT_ROOT"] == str(
        paths.data_root
    )
    assert configured["DJANGO_COURSE_INSIGHT_DOTENV_PATH"] == str(
        paths.data_root / ".env"
    )

