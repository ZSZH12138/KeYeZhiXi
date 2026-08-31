"""Build the self-contained Windows onedir release and copy product files."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist" / "课业智析-可执行版"
CONDA_RUNTIME_DLLS = ("ffi.dll", "sqlite3.dll")
REQUIRED_PRIVACY_RUNTIME_PACKAGES = (
    "presidio_analyzer",
    "spacy",
    "zh_core_web_sm",
)
REQUIRED_PRIVACY_RUNTIME_DATA_PACKAGES = ("spacy_pkuseg",)
REQUIRED_PRIVACY_RUNTIME_FILES = (
    "_internal/spacy_pkuseg/dicts/default.pkl",
)


def main() -> int:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "course_insight.spec",
        ],
        cwd=PROJECT_ROOT,
        env=build_environment(),
        check=True,
    )
    verify_privacy_runtime_data_files(DIST_DIR)
    _copy_file("启动课业智析.cmd")
    _copy_file("停止课业智析.cmd")
    _copy_file("README.md")
    _copy_file("使用说明.md")
    config_dir = DIST_DIR / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "app.example.json",
        "roles.example.csv",
        "state.json",
        "teacher.json",
    ):
        shutil.copy2(PROJECT_ROOT / "config" / name, config_dir / name)
    contracts = DIST_DIR / "contracts"
    if contracts.exists():
        shutil.rmtree(contracts)
    shutil.copytree(PROJECT_ROOT / "contracts", contracts)
    return 0


def build_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Provide the exact Django setting expected by PyInstaller's hook."""

    environment = dict(os.environ if source is None else source)
    return {
        **environment,
        "DJANGO_SETTINGS_MODULE": "course_insight.web_project.settings",
    }


def required_conda_runtime_binaries(prefix: str | Path) -> list[tuple[str, str]]:
    """Return DLLs that PyInstaller cannot resolve from a Conda environment."""

    library_bin = Path(prefix) / "Library" / "bin"
    binaries = [
        (str(library_bin / name), ".")
        for name in CONDA_RUNTIME_DLLS
        if (library_bin / name).is_file()
    ]
    if len(binaries) != len(CONDA_RUNTIME_DLLS):
        missing = [
            name for name in CONDA_RUNTIME_DLLS if not (library_bin / name).is_file()
        ]
        raise FileNotFoundError(
            f"Conda 构建环境缺少运行库：{', '.join(missing)}"
        )
    return binaries


def verify_privacy_runtime_data_files(dist_dir: str | Path) -> None:
    """Fail the release build if required non-Python NLP data is missing."""

    root = Path(dist_dir)
    missing = [
        relative
        for relative in REQUIRED_PRIVACY_RUNTIME_FILES
        if not (root / Path(relative)).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "Windows release is missing privacy runtime data: "
            + ", ".join(missing)
        )


def _copy_file(name: str) -> None:
    shutil.copy2(PROJECT_ROOT / name, DIST_DIR / name)


if __name__ == "__main__":
    raise SystemExit(main())
