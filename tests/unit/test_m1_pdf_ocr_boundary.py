from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter
import pytest

from course_insight.modules.m1_course_governance.parsers import (
    OCRRequiredError,
    ParsedSource,
    default_parser_registry,
    parse_source,
)


def _image_only_pdf() -> bytes:
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(stream)
    return stream.getvalue()


class _Provider:
    def extract_text(self, payload: bytes, *, page_number: int) -> str:
        assert payload.startswith(b"%PDF")
        assert page_number == 1
        return "OCR extracted lesson text"


def test_image_only_pdf_requires_explicit_ocr_provider() -> None:
    with pytest.raises(OCRRequiredError) as raised:
        parse_source("scan.pdf", _image_only_pdf())

    assert raised.value.code == "COURSE_PDF_OCR_REQUIRED"


def test_pdf_ocr_provider_is_injectable_and_preserves_page_locator() -> None:
    parsed = parse_source(
        "scan.pdf",
        _image_only_pdf(),
        ocr_provider=_Provider(),
    )

    assert isinstance(parsed, ParsedSource)
    assert parsed.blocks[0].text == "OCR extracted lesson text"
    assert parsed.blocks[0].locator == "page:1;block:1"


def test_default_registry_remains_byte_parser_without_hidden_ocr() -> None:
    entry = default_parser_registry().resolve("scan.pdf")
    assert "ocr" not in entry.capabilities
    with pytest.raises(OCRRequiredError):
        entry.parse("scan.pdf", _image_only_pdf())
