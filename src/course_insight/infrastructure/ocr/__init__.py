"""Optional, deployment-provided OCR adapters for M1."""

from course_insight.infrastructure.ocr.tesseract import (
    OCRProviderError,
    TesseractOCRProvider,
)

__all__ = ["OCRProviderError", "TesseractOCRProvider"]
