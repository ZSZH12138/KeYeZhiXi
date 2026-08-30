"""Resolve immutable program resources and per-user mutable data paths."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class DesktopPathError(RuntimeError):
    """Raised when the desktop application cannot select safe paths."""


@dataclass(frozen=True, slots=True)
class DesktopPaths:
    """Locations used by every process in one desktop installation."""

    data_root: Path
    config_dir: Path
    runtime_dir: Path
    logs_dir: Path
    resources_root: Path


def resolve_desktop_paths(
    environment: Mapping[str, str] | None = None,
    *,
    resources_root: Path | str | None = None,
) -> DesktopPaths:
    """Return safe, deterministic desktop paths without creating them."""

    values = os.environ if environment is None else environment
    local_value = values.get("LOCALAPPDATA")
    if not isinstance(local_value, str) or not local_value.strip():
        raise DesktopPathError("本地应用数据目录不可用")

    local_root = Path(local_value).expanduser().resolve()
    data_root = (local_root / "KeYeZhiXi").resolve()
    resource_root = (
        _default_resources_root()
        if resources_root is None
        else Path(resources_root).expanduser().resolve()
    )
    if (
        data_root == resource_root
        or data_root.is_relative_to(resource_root)
        or resource_root.is_relative_to(data_root)
    ):
        raise DesktopPathError("运行数据不能位于程序目录中")

    return DesktopPaths(
        data_root=data_root,
        config_dir=data_root / "config",
        runtime_dir=data_root / "runtime",
        logs_dir=data_root / "logs",
        resources_root=resource_root,
    )


def desktop_environment(paths: DesktopPaths) -> dict[str, str]:
    """Build the non-secret environment shared by Web and Worker processes."""

    return {
        "DJANGO_COURSE_INSIGHT_PROJECT_ROOT": str(paths.data_root),
        "DJANGO_COURSE_INSIGHT_DOTENV_PATH": str(paths.data_root / ".env"),
        "DJANGO_COURSE_INSIGHT_APP_JSON_PATH": str(
            paths.config_dir / "app.json"
        ),
        "DJANGO_SETTINGS_MODULE": "course_insight.web_project.settings",
    }


def _default_resources_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parents[3]
