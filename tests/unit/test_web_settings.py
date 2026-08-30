from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.web_project import settings as web_settings


def test_desktop_project_root_accepts_an_absolute_local_data_path(
    tmp_path: Path,
) -> None:
    default = tmp_path / "source"
    configured = tmp_path / "LocalAppData" / "KeYeZhiXi"

    selected = web_settings._resolve_project_root(  # noqa: SLF001
        {"DJANGO_COURSE_INSIGHT_PROJECT_ROOT": str(configured)},
        default=default,
    )

    assert selected == configured.resolve()


def test_desktop_project_root_rejects_a_relative_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="desktop project root must be absolute"):
        web_settings._resolve_project_root(  # noqa: SLF001
            {"DJANGO_COURSE_INSIGHT_PROJECT_ROOT": "relative/data"},
            default=tmp_path,
        )


def test_desktop_project_root_rejects_a_filesystem_root() -> None:
    root = Path(Path.cwd().anchor)

    with pytest.raises(RuntimeError, match="desktop project root is unsafe"):
        web_settings._resolve_project_root(  # noqa: SLF001
            {"DJANGO_COURSE_INSIGHT_PROJECT_ROOT": str(root)},
            default=Path.cwd(),
        )


def test_source_project_root_remains_the_default(tmp_path: Path) -> None:
    assert web_settings._resolve_project_root(  # noqa: SLF001
        {},
        default=tmp_path,
    ) == tmp_path.resolve()
