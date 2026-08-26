"""Bounded validation for teacher-uploaded knowledge and question files."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath, PurePosixPath
from zipfile import BadZipFile, ZipFile

from django.core.files.uploadedfile import UploadedFile


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4_096
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200

_KNOWLEDGE_EXTENSIONS = frozenset(
    {".md", ".txt", ".pdf", ".ppt", ".pptx", ".docx"}
)
_TEXT_EXTENSIONS = frozenset({".md", ".txt"})
_OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_DANGEROUS_EXTENSIONS = frozenset(
    {".exe", ".com", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr"}
)
_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class FileValidationError(ValueError):
    """Safe upload rejection suitable for a teacher-facing form error."""


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    display_name: str
    extension: str
    media_type: str
    payload: bytes
    sha256: str


def normalize_display_name(value: str) -> str:
    """Return a Unicode-normalized basename without trusting client paths."""

    if not isinstance(value, str):
        raise FileValidationError("file name is invalid")
    basename = value.replace("\\", "/").rsplit("/", 1)[-1]
    normalized = unicodedata.normalize("NFKC", basename).strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or len(normalized) > 255
        or any(ord(character) < 32 or character == "\x7f" for character in normalized)
    ):
        raise FileValidationError("file name is invalid")
    return normalized


def validate_uploaded_file(
    uploaded: UploadedFile,
    *,
    source_type: str,
    max_bytes: int = MAX_FILE_BYTES,
) -> ValidatedUpload:
    """Validate extension, bytes and archive structure before persistence."""

    if source_type not in {"knowledge", "question"}:
        raise FileValidationError("source type is invalid")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    display_name = normalize_display_name(uploaded.name)
    suffixes = [suffix.casefold() for suffix in PurePath(display_name).suffixes]
    extension = suffixes[-1] if suffixes else ""
    if source_type == "question" and extension != ".txt":
        raise FileValidationError("question files must use the .txt format")
    if source_type == "knowledge" and extension not in _KNOWLEDGE_EXTENSIONS:
        raise FileValidationError("knowledge file type is not supported")
    if len(suffixes) > 1 and any(
        suffix in _KNOWLEDGE_EXTENSIONS or suffix in _DANGEROUS_EXTENSIONS
        for suffix in suffixes[:-1]
    ):
        raise FileValidationError("double-extension file names are not accepted")

    payload = _read_bounded(uploaded, max_bytes)
    if not payload:
        raise FileValidationError("empty files are not accepted")
    if extension in _TEXT_EXTENSIONS:
        _validate_utf8_text(payload)
    elif extension == ".pdf":
        if not payload.startswith(b"%PDF-"):
            raise FileValidationError("PDF signature is invalid")
    elif extension == ".ppt":
        if not payload.startswith(_OLE_COMPOUND_FILE_SIGNATURE):
            raise FileValidationError("PowerPoint signature is invalid")
    elif extension in {".pptx", ".docx"}:
        _validate_ooxml(payload, extension)

    return ValidatedUpload(
        display_name=display_name,
        extension=extension,
        media_type=_MEDIA_TYPES[extension],
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _read_bounded(uploaded: UploadedFile, maximum: int) -> bytes:
    stream = BytesIO()
    for chunk in uploaded.chunks(chunk_size=64 * 1024):
        if stream.tell() + len(chunk) > maximum:
            raise FileValidationError("uploaded file is too large")
        stream.write(chunk)
    return stream.getvalue()


def _validate_utf8_text(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileValidationError("text files must use UTF-8 encoding") from None
    if "\x00" in text:
        raise FileValidationError("text file contains unsupported null bytes")


def _validate_ooxml(payload: bytes, extension: str) -> None:
    required = "ppt/presentation.xml" if extension == ".pptx" else "word/document.xml"
    try:
        with ZipFile(BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(members) > MAX_ARCHIVE_MEMBERS or len(names) != len(set(names)):
                raise FileValidationError("unsafe archive structure")
            total_size = 0
            for member in members:
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if (
                    member.flag_bits & 1
                    or member.filename.startswith(("/", "\\"))
                    or ":" in member.filename
                    or ".." in path.parts
                ):
                    raise FileValidationError("unsafe archive structure")
                total_size += member.file_size
                if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise FileValidationError("unsafe archive expansion")
                if member.file_size > max(1, member.compress_size) * MAX_COMPRESSION_RATIO:
                    raise FileValidationError("unsafe archive compression ratio")
            if "[Content_Types].xml" not in names or required not in names:
                raise FileValidationError("office file container is invalid")
    except (BadZipFile, RuntimeError):
        raise FileValidationError("office file container is invalid") from None
