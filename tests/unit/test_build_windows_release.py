from __future__ import annotations

import pytest

from scripts.build_windows_release import (
    REQUIRED_PRIVACY_RUNTIME_FILES,
    REQUIRED_PRIVACY_RUNTIME_DATA_PACKAGES,
    REQUIRED_PRIVACY_RUNTIME_PACKAGES,
    build_environment,
    required_conda_runtime_binaries,
    verify_privacy_runtime_data_files,
)


def test_pyinstaller_receives_the_package_qualified_django_settings() -> None:
    environment = build_environment({"PATH": "existing-path"})

    assert environment == {
        "PATH": "existing-path",
        "DJANGO_SETTINGS_MODULE": "course_insight.web_project.settings",
    }


def test_conda_runtime_dlls_are_explicitly_added_to_the_bundle(tmp_path) -> None:
    library_bin = tmp_path / "Library" / "bin"
    library_bin.mkdir(parents=True)
    ffi = library_bin / "ffi.dll"
    sqlite = library_bin / "sqlite3.dll"
    ffi.write_bytes(b"ffi")
    sqlite.write_bytes(b"sqlite")

    binaries = required_conda_runtime_binaries(tmp_path)

    assert binaries == [
        (str(ffi), "."),
        (str(sqlite), "."),
    ]


def test_windows_bundle_declares_the_local_privacy_runtime() -> None:
    assert REQUIRED_PRIVACY_RUNTIME_PACKAGES == (
        "presidio_analyzer",
        "spacy",
        "zh_core_web_sm",
    )
    assert REQUIRED_PRIVACY_RUNTIME_DATA_PACKAGES == ("spacy_pkuseg",)
    assert REQUIRED_PRIVACY_RUNTIME_FILES == (
        "_internal/spacy_pkuseg/dicts/default.pkl",
    )


def test_windows_bundle_verifies_required_privacy_data(tmp_path) -> None:
    required = tmp_path / REQUIRED_PRIVACY_RUNTIME_FILES[0]
    required.parent.mkdir(parents=True)
    required.write_bytes(b"pkuseg")

    verify_privacy_runtime_data_files(tmp_path)

    required.unlink()
    with pytest.raises(FileNotFoundError, match="default.pkl"):
        verify_privacy_runtime_data_files(tmp_path)
