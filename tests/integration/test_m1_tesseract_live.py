from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

import pytest

from course_insight.infrastructure.ocr import TesseractOCRProvider
from course_insight.modules.m1_course_governance.parsers import parse_source


_EXECUTABLE = os.environ.get("M1_LIVE_OCR_EXECUTABLE")
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - live OCR extra not installed
    Image = ImageDraw = ImageFont = None  # type: ignore[misc, assignment]
try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - compatibility with older PyMuPDF
    try:
        import fitz  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - OCR extra not installed
        fitz = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    not _EXECUTABLE or Image is None or fitz is None,
    reason="set M1_LIVE_OCR_EXECUTABLE and install the [ocr] extra to run live OCR",
)


def _build_scanned_pdf() -> bytes:
    image = Image.new("RGB", (1800, 500), "white")
    draw = ImageDraw.Draw(image)
    font_candidates = (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
    )
    font_path = next((path for path in font_candidates if path.exists()), None)
    font = ImageFont.truetype(str(font_path), 58) if font_path else None
    draw.text((60, 70), "Course OCR Contract 123", fill="black", font=font)
    draw.text((60, 230), "课程治理", fill="black", font=font)

    image_bytes = BytesIO()
    image.save(image_bytes, format="PNG")
    document = fitz.open()
    page = document.new_page(width=900, height=250)
    page.insert_image(page.rect, stream=image_bytes.getvalue())
    payload = document.tobytes()
    document.close()
    return payload


def test_live_tesseract_extracts_scanned_pdf_text() -> None:
    provider = TesseractOCRProvider(
        executable=_EXECUTABLE,
        language=os.environ.get("M1_LIVE_OCR_LANGUAGE", "chi_sim+eng"),
        dpi=200,
    )

    parsed = parse_source(
        "scanned-course.pdf",
        _build_scanned_pdf(),
        ocr_provider=provider,
    )

    assert parsed.page_count == 1
    assert parsed.blocks
    assert any("OCR" in block.text or "Course" in block.text for block in parsed.blocks)
    assert parsed.blocks[0].locator.startswith("page:1;block:")
