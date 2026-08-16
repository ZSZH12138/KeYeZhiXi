"""Private immutable inputs retained by a successful governed M1 import."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourcePayload:
    source_id: str
    file_name: str
    raw_bytes: bytes


@dataclass(frozen=True, slots=True)
class CourseImportSnapshot:
    course_metadata_bytes: bytes
    source_authorization_bytes: bytes
    source_payloads: tuple[SourcePayload, ...]
