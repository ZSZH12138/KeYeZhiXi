# -*- mode: python ; coding: utf-8 -*-

import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
from scripts.build_windows_release import (
    REQUIRED_PRIVACY_RUNTIME_DATA_PACKAGES,
    REQUIRED_PRIVACY_RUNTIME_PACKAGES,
    required_conda_runtime_binaries,
)


hiddenimports = collect_submodules("course_insight")
datas = collect_data_files("course_insight")
binaries = required_conda_runtime_binaries(sys.prefix)
for package_name in REQUIRED_PRIVACY_RUNTIME_PACKAGES:
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)
for package_name in REQUIRED_PRIVACY_RUNTIME_DATA_PACKAGES:
    datas.extend(collect_data_files(package_name))

analysis = Analysis(
    ["src/course_insight/desktop_launcher.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pytest_cov"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="课业智析",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="课业智析-可执行版",
)
