"""Bounded local Tesseract OCR adapter for M1 image-only PDFs.

The core parser remains dependency-free with respect to OCR.  Deployments opt
into this adapter by installing the ``ocr`` extra and providing a Tesseract
executable with the required language data.  The adapter accepts PDF bytes and
returns text for one page; it never accepts user-controlled executable flags or
host paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Protocol


_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_.+:-]{1,64}$")
_DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def _load_pymupdf() -> object:
    """Load the current PyMuPDF module name with a compatibility fallback."""

    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore[import-not-found]
        except ImportError:
            raise OCRProviderError(
                "PyMuPDF is required for the configured OCR provider"
            ) from None
    return pymupdf


class OCRProviderError(RuntimeError):
    """A safe, recoverable OCR adapter failure."""


class PageRenderer(Protocol):
    """Render one PDF page to an in-memory PNG."""

    def render_page(self, payload: bytes, *, page_number: int, dpi: int) -> bytes:
        """Return PNG bytes for a one-based page number."""


class TextRecognizer(Protocol):
    """Recognize text from one rendered page image."""

    def recognize(
        self,
        image_bytes: bytes,
        *,
        language: str,
        timeout_seconds: float,
    ) -> str:
        """Return OCR text without exposing process details."""


@dataclass(frozen=True, slots=True)
class TesseractOCRProvider:
    """M1 OCR provider with injectable renderer and recognizer seams."""

    language: str = "chi_sim+eng"
    dpi: int = 200
    timeout_seconds: float = 30.0
    executable: str | None = field(default=None, repr=False, compare=False)
    max_output_bytes: int = field(
        default=_DEFAULT_MAX_OUTPUT_BYTES,
        repr=False,
        compare=False,
    )
    renderer: PageRenderer | None = field(default=None, repr=False, compare=False)
    recognizer: TextRecognizer | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.language, str)
            or not _LANGUAGE_PATTERN.fullmatch(self.language)
        ):
            raise ValueError("language must contain only safe OCR language identifiers")
        if type(self.dpi) is not int or not 72 <= self.dpi <= 600:
            raise ValueError("dpi must be an integer between 72 and 600")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not 0.1 <= float(self.timeout_seconds) <= 120.0
        ):
            raise ValueError("timeout_seconds must be between 0.1 and 120 seconds")
        if (
            self.executable is not None
            and (
                not isinstance(self.executable, str)
                or not self.executable.strip()
                or len(self.executable) > 512
                or any(character in self.executable for character in ("\r", "\n"))
            )
        ):
            raise ValueError("executable is invalid")
        if (
            type(self.max_output_bytes) is not int
            or not 1 <= self.max_output_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("max_output_bytes is outside the supported range")
        if self.renderer is None:
            _load_pymupdf()
        if self.recognizer is None:
            SubprocessTesseractRecognizer(
                executable=self.executable,
                max_output_bytes=self.max_output_bytes,
            )

    def extract_text(self, payload: bytes, *, page_number: int) -> str:
        """Render and recognize one PDF page, returning non-empty text."""

        if not isinstance(payload, bytes) or not payload:
            raise OCRProviderError("OCR payload must be non-empty bytes")
        if type(page_number) is not int or page_number < 1:
            raise OCRProviderError("OCR page number must be a positive integer")

        renderer = self.renderer or PyMuPDFPageRenderer()
        recognizer = self.recognizer or SubprocessTesseractRecognizer(
            executable=self.executable,
            max_output_bytes=self.max_output_bytes,
        )
        try:
            image_bytes = renderer.render_page(
                payload,
                page_number=page_number,
                dpi=self.dpi,
            )
            if not isinstance(image_bytes, bytes) or not image_bytes:
                raise OCRProviderError("OCR renderer returned no image")
            text = recognizer.recognize(
                image_bytes,
                language=self.language,
                timeout_seconds=float(self.timeout_seconds),
            )
        except OCRProviderError:
            raise
        except Exception as error:
            raise OCRProviderError("OCR extraction failed") from error
        if not isinstance(text, str) or not text.strip():
            raise OCRProviderError("OCR result is empty")
        return text


class PyMuPDFPageRenderer:
    """Render PDF pages using the optional PyMuPDF dependency."""

    def render_page(self, payload: bytes, *, page_number: int, dpi: int) -> bytes:
        fitz = _load_pymupdf()

        document = None
        try:
            document = fitz.open(stream=payload, filetype="pdf")
            if page_number > document.page_count:
                raise OCRProviderError("OCR page number exceeds PDF page count")
            page = document.load_page(page_number - 1)
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            image_bytes = pixmap.tobytes("png")
            if not image_bytes:
                raise OCRProviderError("OCR renderer returned no image")
            return image_bytes
        except OCRProviderError:
            raise
        except Exception as error:
            raise OCRProviderError("OCR PDF rendering failed") from error
        finally:
            if document is not None:
                document.close()


class SubprocessTesseractRecognizer:
    """Run a fixed Tesseract command with timeout and bounded output."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_output_bytes is outside the supported range")
        resolved = (
            executable
            if executable is not None and Path(executable).is_file()
            else shutil.which(executable or "tesseract")
        )
        if not isinstance(resolved, str) or not resolved:
            raise OCRProviderError("Tesseract executable is unavailable")
        self._executable = str(Path(resolved))
        self._max_output_bytes = max_output_bytes

    def recognize(
        self,
        image_bytes: bytes,
        *,
        language: str,
        timeout_seconds: float,
    ) -> str:
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise OCRProviderError("OCR image is empty")
        if not isinstance(language, str) or not _LANGUAGE_PATTERN.fullmatch(language):
            raise OCRProviderError("OCR language is invalid")
        with TemporaryDirectory(prefix="course_insight_ocr_") as temporary_directory:
            image_path = Path(temporary_directory) / "page.png"
            image_path.write_bytes(image_bytes)
            command = [
                self._executable,
                str(image_path),
                "stdout",
                "-l",
                language,
                "--psm",
                "6",
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    timeout=float(timeout_seconds),
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        if os.name == "nt"
                        else 0
                    ),
                )
            except subprocess.TimeoutExpired as error:
                raise OCRProviderError("OCR process timed out") from error
            except OSError as error:
                raise OCRProviderError("OCR process could not start") from error
        if completed.returncode != 0:
            raise OCRProviderError("OCR process failed")
        if len(completed.stdout) > self._max_output_bytes:
            raise OCRProviderError("OCR output exceeds the configured limit")
        return completed.stdout.decode("utf-8", errors="replace")


__all__ = [
    "OCRProviderError",
    "PageRenderer",
    "PyMuPDFPageRenderer",
    "SubprocessTesseractRecognizer",
    "TesseractOCRProvider",
    "TextRecognizer",
]
