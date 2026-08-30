from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_windows_builder_exposes_a_safe_onedir_distribution_contract() -> None:
    project_root = Path(__file__).resolve().parents[2]
    powershell = shutil.which("pwsh")
    assert powershell is not None

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(project_root / "scripts" / "build_windows_app.ps1"),
            "-ValidateOnly",
            "-PythonExecutable",
            sys.executable,
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    contract = json.loads(completed.stdout)
    assert contract == {
        "appName": "KeYeZhiXi",
        "contentsDirectory": "_internal",
        "entrypoint": "src/course_insight/desktop/__main__.py",
        "executable": "KeYeZhiXi.exe",
        "mode": "onedir",
        "mutableDataIncluded": False,
        "nativeRuntimeLibraries": [
            "ffi.dll",
            "sqlite3.dll",
            "tcl86t.dll",
            "tk86t.dll",
        ],
        "resources": [
            "config/app.example.json",
            "config/state.json",
            "config/teacher.json",
        ],
        "toolDirectory": ".build-tools/pyinstaller",
    }
