# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


packaging_dir = Path(SPECPATH).resolve()
project_root = packaging_dir.parent
manifest = json.loads(
    (packaging_dir / "desktop_manifest.json").read_text(encoding="utf-8")
)

resource_data = [
    (
        str(project_root / resource["source"]),
        resource["destination"],
    )
    for resource in manifest["resources"]
]
package_data = collect_data_files("course_insight")
package_metadata = copy_metadata("course-insight")

analysis = Analysis(
    [str(project_root / manifest["entrypoint"])],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=package_data + package_metadata + resource_data,
    hiddenimports=collect_submodules("course_insight"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["IPython", "jupyter", "notebook", "pytest"],
    noarchive=False,
    optimize=0,
)

python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=manifest["appName"],
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory=manifest["contentsDirectory"],
)

distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=manifest["appName"],
)
