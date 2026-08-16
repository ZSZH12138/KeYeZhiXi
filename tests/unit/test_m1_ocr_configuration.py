from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.application.factory import ServiceOverrides, _build_ocr_provider
from course_insight.infrastructure.config import load_platform_settings
from course_insight.infrastructure.config.errors import ConfigurationError


def test_ocr_is_disabled_by_default(tmp_path: Path) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={},
    )

    assert settings.ocr.backend == "disabled"
    assert settings.ocr.language == "chi_sim+eng"
    assert settings.ocr.dpi == 200


def test_tesseract_ocr_settings_are_loaded_from_environment(tmp_path: Path) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "COURSE_INSIGHT_OCR__BACKEND": "tesseract",
            "COURSE_INSIGHT_OCR__EXECUTABLE": "C:/tools/tesseract.exe",
            "COURSE_INSIGHT_OCR__LANGUAGE": "chi_sim+eng",
            "COURSE_INSIGHT_OCR__DPI": "220",
            "COURSE_INSIGHT_OCR__TIMEOUT_SECONDS": "15",
        },
    )

    assert settings.ocr.backend == "tesseract"
    assert settings.ocr.executable == "C:/tools/tesseract.exe"
    assert settings.ocr.dpi == 220
    assert settings.ocr.timeout_seconds == 15.0


def test_disabled_ocr_rejects_an_executable_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as error:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={
                "COURSE_INSIGHT_OCR__BACKEND": "disabled",
                "COURSE_INSIGHT_OCR__EXECUTABLE": "C:/tools/tesseract.exe",
            },
        )

    assert error.value.code == "INVALID_OCR_CONFIGURATION"


def test_factory_builds_configured_tesseract_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "COURSE_INSIGHT_OCR__BACKEND": "tesseract",
            "COURSE_INSIGHT_OCR__EXECUTABLE": "C:/tools/tesseract.exe",
            "COURSE_INSIGHT_OCR__LANGUAGE": "chi_sim+eng",
            "COURSE_INSIGHT_OCR__DPI": "240",
            "COURSE_INSIGHT_OCR__TIMEOUT_SECONDS": "20",
        },
    )

    class _FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(
        "course_insight.application.factory.TesseractOCRProvider",
        _FakeProvider,
    )
    provider = _build_ocr_provider(settings, ServiceOverrides())

    assert isinstance(provider, _FakeProvider)
    assert provider.kwargs == {
        "executable": "C:/tools/tesseract.exe",
        "language": "chi_sim+eng",
        "dpi": 240,
        "timeout_seconds": 20.0,
        "max_output_bytes": 2 * 1024 * 1024,
    }
