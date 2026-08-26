from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from course_insight.modules.m0_platform.django_app.file_validation import (
    FileValidationError,
    normalize_display_name,
    validate_uploaded_file,
)


_OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


def _ooxml(*members: tuple[str, bytes]) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        for name, payload in members:
            archive.writestr(name, payload)
    return stream.getvalue()


def test_valid_markdown_and_real_pptx_are_accepted() -> None:
    markdown = validate_uploaded_file(
        SimpleUploadedFile("chapter.md", "# 拥塞控制".encode()),
        source_type="knowledge",
    )
    pptx = validate_uploaded_file(
        SimpleUploadedFile(
            "slides.pptx",
            _ooxml(("ppt/presentation.xml", b"<p:presentation/>")),
        ),
        source_type="knowledge",
    )

    assert markdown.media_type == "text/markdown"
    assert pptx.media_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def test_legacy_ppt_with_compound_file_signature_is_accepted() -> None:
    result = validate_uploaded_file(
        SimpleUploadedFile(
            "legacy.ppt",
            _OLE_COMPOUND_FILE_SIGNATURE + b"legacy presentation payload",
        ),
        source_type="knowledge",
    )

    assert result.extension == ".ppt"
    assert result.media_type == "application/vnd.ms-powerpoint"


def test_renamed_legacy_ppt_without_compound_file_signature_is_rejected() -> None:
    with pytest.raises(FileValidationError, match="PowerPoint signature"):
        validate_uploaded_file(
            SimpleUploadedFile("renamed.ppt", b"not an OLE presentation"),
            source_type="knowledge",
        )


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("slides.pptx", b"this is not a zip container"),
        ("chapter.txt.pdf", b"%PDF-1.7\n"),
        ("malware.exe", b"MZ"),
    ],
)
def test_false_container_double_extension_and_unsupported_type_are_rejected(name, payload) -> None:
    with pytest.raises(FileValidationError):
        validate_uploaded_file(
            SimpleUploadedFile(name, payload),
            source_type="knowledge",
        )


def test_ooxml_zip_traversal_is_rejected() -> None:
    payload = _ooxml(
        ("ppt/presentation.xml", b"<p:presentation/>"),
        ("../outside.txt", b"unsafe"),
    )

    with pytest.raises(FileValidationError, match="unsafe archive"):
        validate_uploaded_file(
            SimpleUploadedFile("slides.pptx", payload),
            source_type="knowledge",
        )


def test_file_size_is_bounded_before_parsing() -> None:
    with pytest.raises(FileValidationError, match="too large"):
        validate_uploaded_file(
            SimpleUploadedFile("chapter.txt", b"a" * 11),
            source_type="knowledge",
            max_bytes=10,
        )


def test_question_source_accepts_only_utf8_txt() -> None:
    result = validate_uploaded_file(
        SimpleUploadedFile("questions.txt", "[选择题]\n题干：...".encode()),
        source_type="question",
    )
    assert result.media_type == "text/plain"

    with pytest.raises(FileValidationError, match="question"):
        validate_uploaded_file(
            SimpleUploadedFile("questions.md", b"question"),
            source_type="question",
        )


def test_display_name_is_unicode_normalized_and_path_free() -> None:
    assert normalize_display_name("folder\\ＡＢＣ.txt") == "ABC.txt"
