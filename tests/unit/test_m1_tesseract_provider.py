from __future__ import annotations

import pytest

from course_insight.infrastructure.ocr.tesseract import (
    OCRProviderError,
    TesseractOCRProvider,
)


class _Renderer:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int, int]] = []

    def render_page(self, payload: bytes, *, page_number: int, dpi: int) -> bytes:
        self.calls.append((payload, page_number, dpi))
        return b"rendered-page"


class _Recognizer:
    def __init__(self, text: str = "识别出的课程内容") -> None:
        self.calls: list[tuple[bytes, str, float]] = []
        self.text = text

    def recognize(
        self,
        image_bytes: bytes,
        *,
        language: str,
        timeout_seconds: float,
    ) -> str:
        self.calls.append((image_bytes, language, timeout_seconds))
        return self.text


def test_tesseract_provider_renders_requested_page_and_returns_text() -> None:
    renderer = _Renderer()
    recognizer = _Recognizer()
    provider = TesseractOCRProvider(
        language="chi_sim+eng",
        dpi=220,
        timeout_seconds=12.0,
        renderer=renderer,
        recognizer=recognizer,
    )

    result = provider.extract_text(b"pdf-bytes", page_number=2)

    assert result == "识别出的课程内容"
    assert renderer.calls == [(b"pdf-bytes", 2, 220)]
    assert recognizer.calls == [(b"rendered-page", "chi_sim+eng", 12.0)]


def test_tesseract_provider_rejects_invalid_page_and_empty_payload() -> None:
    provider = TesseractOCRProvider(renderer=_Renderer(), recognizer=_Recognizer())

    with pytest.raises(OCRProviderError, match="payload"):
        provider.extract_text(b"", page_number=1)
    with pytest.raises(OCRProviderError, match="page number"):
        provider.extract_text(b"pdf-bytes", page_number=0)


def test_tesseract_provider_rejects_empty_recognizer_output() -> None:
    provider = TesseractOCRProvider(
        renderer=_Renderer(),
        recognizer=_Recognizer(text="   "),
    )

    with pytest.raises(OCRProviderError, match="empty"):
        provider.extract_text(b"pdf-bytes", page_number=1)


def test_tesseract_provider_validates_runtime_limits() -> None:
    with pytest.raises(ValueError, match="dpi"):
        TesseractOCRProvider(dpi=0)
    with pytest.raises(ValueError, match="timeout"):
        TesseractOCRProvider(timeout_seconds=0)


def test_tesseract_provider_rejects_missing_executable() -> None:
    with pytest.raises(OCRProviderError, match="executable"):
        TesseractOCRProvider(executable="C:/missing/tesseract.exe")
