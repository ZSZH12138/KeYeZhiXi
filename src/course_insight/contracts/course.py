"""M1 course-source, authorization, chunk, and package contracts."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


class SourceAuthorization(ContractModel):
    """Authorization evidence for one governed course source."""

    source_id: str = Field(min_length=1)
    authorized_by: str = Field(min_length=1)
    license_note: str = Field(min_length=1)
    authorized_at: datetime

    def covers(self, source_id: str) -> bool:
        """Return whether this authorization names the requested source."""

        return self.source_id == source_id


class SourceDocument(ContractModel):
    """Metadata and immutable content identity for an imported file."""

    source_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    sha256: str = Field(min_length=1)
    page_count: int | None = Field(ge=1)
    title: str = Field(min_length=1)
    version: str = Field(min_length=1)

    def matches_file(self, path: Path) -> bool:
        """Compare both the governed file name and its streamed SHA-256."""

        if not path.is_file() or path.name != self.file_name:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(64 * 1024), b""):
                digest.update(block)
        return hmac.compare_digest(digest.hexdigest(), self.sha256.casefold())


class ContentChunk(ContractModel):
    """Traceable text slice produced from a governed source document."""

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    text: str
    locator: str = Field(min_length=1)
    concept_hints: list[str]
    sha256: str = Field(min_length=1)

    def contains(self, term: str) -> bool:
        """Search case-insensitively after deterministic whitespace folding."""

        normalized_text = " ".join(self.text.split()).casefold()
        normalized_term = " ".join(term.split()).casefold()
        return bool(normalized_term) and normalized_term in normalized_text

    def word_count(self) -> int:
        """Count whitespace-delimited tokens deterministically."""

        return len(self.text.split())


class CoursePackage(ContractModel):
    """Validated M1 output consumed directly by M2 and M3."""

    course_package_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    source_documents: list[SourceDocument]
    content_chunks: list[ContentChunk]
    source_authorizations: list[SourceAuthorization]
    imported_at: datetime
    status: Literal["draft", "ready", "failed"]
    checksum: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Validate identifiers, references, and the ready-state minimum."""

        source_ids = [source.source_id for source in self.source_documents]
        if len(source_ids) != len(set(source_ids)):
            raise DomainError(
                code="DUPLICATE_SOURCE_ID",
                module="m1",
                message="course package source identifiers must be unique",
            )
        chunk_ids = [chunk.chunk_id for chunk in self.content_chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise DomainError(
                code="DUPLICATE_CHUNK_ID",
                module="m1",
                message="course package chunk identifiers must be unique",
            )

        source_id_set = set(source_ids)
        missing_chunk_sources = sorted(
            {chunk.source_id for chunk in self.content_chunks} - source_id_set
        )
        if missing_chunk_sources:
            raise DomainError(
                code="CHUNK_SOURCE_NOT_FOUND",
                module="m1",
                message="every content chunk must reference an imported source",
                details={"source_ids": missing_chunk_sources},
            )

        missing_authorization_sources = sorted(
            {authorization.source_id for authorization in self.source_authorizations}
            - source_id_set
        )
        if missing_authorization_sources:
            raise DomainError(
                code="AUTHORIZATION_SOURCE_NOT_FOUND",
                module="m1",
                message="every authorization must reference an imported source",
                details={"source_ids": missing_authorization_sources},
            )

        if self.status == "ready" and (
            not self.source_documents or not self.content_chunks
        ):
            raise DomainError(
                code="READY_PACKAGE_CONTENT_REQUIRED",
                module="m1",
                message="a ready course package requires sources and chunks",
            )

    def find_source(self, source_id: str) -> SourceDocument:
        """Return the source with the requested identifier."""

        for source in self.source_documents:
            if source.source_id == source_id:
                return source.model_copy(deep=True)
        raise DomainError(
            code="SOURCE_NOT_FOUND",
            module="m1",
            message="course source was not found",
            details={"source_id": source_id},
        )

    def find_chunk(self, chunk_id: str) -> ContentChunk:
        """Return the chunk with the requested identifier."""

        for chunk in self.content_chunks:
            if chunk.chunk_id == chunk_id:
                return chunk.model_copy(deep=True)
        raise DomainError(
            code="CHUNK_NOT_FOUND",
            module="m1",
            message="course content chunk was not found",
            details={"chunk_id": chunk_id},
        )

    def list_chunks_by_source(self, source_id: str) -> list[ContentChunk]:
        """Return a new list in import order without changing package state."""

        return [
            chunk.model_copy(deep=True)
            for chunk in self.content_chunks
            if chunk.source_id == source_id
        ]

    def recalculate_checksum(self) -> str:
        """Return the canonical checksum without mutating the stored checksum."""

        return self.content_checksum()
