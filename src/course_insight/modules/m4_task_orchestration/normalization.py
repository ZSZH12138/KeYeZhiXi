"""Versioned text normalization shared by every private M4 intent boundary."""

from __future__ import annotations

import unicodedata


NORMALIZATION_VERSION = "m4-text-normalization-v1"


def normalize_intent_text(text: str) -> str:
    """Return NFKC text with surrounding/duplicate whitespace folded and cased."""

    if not isinstance(text, str):
        raise ValueError("intent text must be a string")
    compatible = unicodedata.normalize("NFKC", text)
    return " ".join(compatible.split()).casefold()
