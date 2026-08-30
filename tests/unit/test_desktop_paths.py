from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.desktop.paths import (
    DesktopPathError,
    desktop_environment,
    resolve_desktop_paths,
)


def test_desktop_paths_keep_mutable_data_outside_the_bundle(
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    bundle = tmp_path / "ReadOnlyBundle"

    paths = resolve_desktop_paths(
        {"LOCALAPPDATA": str(local_app_data)},
        resources_root=bundle,
    )

    assert paths.data_root == (local_app_data / "KeYeZhiXi").resolve()
    assert paths.config_dir == paths.data_root / "config"
    assert paths.runtime_dir == paths.data_root / "runtime"
    assert paths.logs_dir == paths.data_root / "logs"
    assert paths.resources_root == bundle.resolve()
    assert not paths.data_root.is_relative_to(paths.resources_root)


def test_desktop_paths_fail_closed_without_local_app_data() -> None:
    with pytest.raises(DesktopPathError, match="本地应用数据目录不可用"):
        resolve_desktop_paths({}, resources_root=Path("C:/bundle"))


@pytest.mark.parametrize("value", ["", "   "])
def test_desktop_paths_reject_blank_local_app_data(value: str) -> None:
    with pytest.raises(DesktopPathError, match="本地应用数据目录不可用"):
        resolve_desktop_paths(
            {"LOCALAPPDATA": value},
            resources_root=Path("C:/bundle"),
        )


def test_desktop_paths_reject_a_data_directory_inside_the_bundle(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"

    with pytest.raises(DesktopPathError, match="运行数据不能位于程序目录中"):
        resolve_desktop_paths(
            {"LOCALAPPDATA": str(bundle)},
            resources_root=bundle / "KeYeZhiXi" / "resources",
        )


def test_desktop_environment_points_django_at_the_persistent_root(
    tmp_path: Path,
) -> None:
    paths = resolve_desktop_paths(
        {"LOCALAPPDATA": str(tmp_path)},
        resources_root=tmp_path / "bundle",
    )

    environment = desktop_environment(paths)

    assert environment == {
        "DJANGO_COURSE_INSIGHT_PROJECT_ROOT": str(paths.data_root),
        "DJANGO_COURSE_INSIGHT_DOTENV_PATH": str(paths.data_root / ".env"),
        "DJANGO_COURSE_INSIGHT_APP_JSON_PATH": str(
            paths.config_dir / "app.json"
        ),
        "DJANGO_SETTINGS_MODULE": "course_insight.web_project.settings",
    }
