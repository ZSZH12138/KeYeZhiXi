"""M2 evidence-index, query, result-chunk, and bundle contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from course_insight.contracts.base import ContractModel
from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError


_EVIDENCE_ID_PREFIX = "evidence_"


def _exclude_none(value: object) -> bool:
    return value is None


def _exclude_empty_list(value: object) -> bool:
    return value == []


def _has_lone_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_evidence_query_cache_semantics(
    *,
    course_package_id: object,
    course_package_checksum: object,
    query_text: object,
    concept_ids: object,
    required_evidence_ids: object,
    item_id: object,
) -> None:
    values = [
        course_package_id,
        course_package_checksum,
        query_text,
        item_id,
    ]
    for items in (concept_ids, required_evidence_ids):
        if isinstance(items, str):
            values.append(items)
        elif isinstance(items, (list, tuple, set, frozenset)):
            values.extend(items)
    if any(
        isinstance(item, str) and _has_lone_surrogate(item)
        for item in values
    ):
        raise DomainError(
            code="EVIDENCE_QUERY_INVALID",
            module="m2",
            message="evidence query contains invalid Unicode",
        )


def _raise_evidence_id_mismatch() -> None:
    raise DomainError(
        code="EVIDENCE_ID_MISMATCH",
        module="m2",
        message="evidence identity must match its chunk",
    )


def evidence_id_for_chunk(chunk_id: str) -> str:
    """Return the canonical M2 evidence identifier for one chunk."""

    if (
        not isinstance(chunk_id, str)
        or not chunk_id.strip()
        or "\x00" in chunk_id
    ):
        _raise_evidence_id_mismatch()
    return f"{_EVIDENCE_ID_PREFIX}{chunk_id}"


def chunk_id_for_evidence_id(evidence_id: str) -> str:
    """Return the exact chunk ID encoded by a canonical evidence ID."""

    suffix = None
    if isinstance(evidence_id, str) and evidence_id.startswith(
        _EVIDENCE_ID_PREFIX
    ):
        suffix = evidence_id[len(_EVIDENCE_ID_PREFIX) :]
    if (
        suffix is None
        or not suffix.strip()
        or "\x00" in suffix
        or evidence_id_for_chunk(suffix) != evidence_id
    ):
        _raise_evidence_id_mismatch()
    return suffix


class EvidenceIndexRef(ContractModel):
    """Persistent identity and readiness metadata for an M2 index."""

    index_id: str = Field(min_length=1)
    course_package_id: str = Field(min_length=1)
    course_package_checksum: str | None = Field(
        default=None,
        exclude_if=_exclude_none,
    )
    index_version: str = Field(min_length=1)
    storage_ref: str = Field(min_length=1)
    backend: Literal["lexical", "pgvector"] = "lexical"
    embedding_model_id: str | None = None
    source_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    built_at: datetime
    checksum: str = Field(min_length=1)
    status: Literal["building", "ready", "failed", "empty"]

    def validate_business_rules(self) -> None:
        """Keep storage references portable and empty indexes genuinely empty."""

        portable_ref = (
            self.storage_ref.startswith(f"{self.backend}:")
            and "/" not in self.storage_ref
            and "\\" not in self.storage_ref
        )
        empty_is_consistent = self.status != "empty" or (
            self.source_count == 0
            and self.chunk_count == 0
            and self.embedding_model_id is None
        )
        lexical_is_consistent = not (
            self.backend == "lexical" and self.embedding_model_id is not None
        )
        if not (portable_ref and empty_is_consistent and lexical_is_consistent):
            raise DomainError(
                code="EVIDENCE_INDEX_REF_INVALID",
                module="m2",
                message="index storage, backend, and empty state must be consistent",
            )

    def assert_ready(self) -> None:
        """Reject retrieval attempts against an unfinished index."""

        if self.status != "ready":
            raise DomainError(
                code="INDEX_NOT_READY",
                module="m2",
                message="evidence index is not ready for retrieval",
                details={"index_id": self.index_id, "status": self.status},
                recoverable=True,
            )

    def matches(self, course_package: CoursePackage) -> bool:
        """Check package identity and source/chunk counts available to M2."""

        return (
            self.course_package_id == course_package.course_package_id
            and (
                self.course_package_checksum is None
                or self.course_package_checksum == course_package.checksum
            )
            and self.source_count == len(course_package.source_documents)
            and self.chunk_count == len(course_package.content_chunks)
        )


class EvidenceQuery(ContractModel):
    """Bounded evidence request produced by M6 or M8."""

    query_id: str = Field(min_length=1)
    course_package_id: str = Field(min_length=1)
    course_package_checksum: str | None = Field(
        default=None,
        exclude_if=_exclude_none,
    )
    query_text: str = Field(min_length=1)
    concept_ids: list[str]
    required_evidence_ids: list[str] = Field(
        default_factory=list,
        exclude_if=_exclude_empty_list,
    )
    item_id: str | None
    use_case: Literal["grading", "feedback", "qa"]
    top_k: int = Field(ge=1)
    min_relevance: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def _reject_lone_surrogate_query_semantics(cls, value: Any) -> Any:
        """Fail closed before JSON cache encoding can encounter surrogates."""

        if not isinstance(value, Mapping):
            return value
        _validate_evidence_query_cache_semantics(
            course_package_id=value.get("course_package_id"),
            course_package_checksum=value.get("course_package_checksum"),
            query_text=value.get("query_text"),
            concept_ids=value.get("concept_ids"),
            required_evidence_ids=value.get("required_evidence_ids"),
            item_id=value.get("item_id"),
        )
        return value

    def normalized_text(self) -> str:
        """Fold whitespace and case without altering the stored query."""

        return " ".join(self.query_text.split()).casefold()

    def cache_key(self) -> str:
        """Hash query semantics while excluding the per-request query ID."""

        _validate_evidence_query_cache_semantics(
            course_package_id=self.course_package_id,
            course_package_checksum=self.course_package_checksum,
            query_text=self.query_text,
            concept_ids=self.concept_ids,
            required_evidence_ids=self.required_evidence_ids,
            item_id=self.item_id,
        )
        payload = {
            "concept_ids": sorted(set(self.concept_ids)),
            "course_package_id": self.course_package_id,
            "course_package_checksum": self.course_package_checksum,
            "item_id": self.item_id,
            "min_relevance": self.min_relevance,
            "query_text": self.normalized_text(),
            "required_evidence_ids": sorted(set(self.required_evidence_ids)),
            "top_k": self.top_k,
            "use_case": self.use_case,
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class EvidenceChunk(ContractModel):
    """One retrieved course quotation with stable source location."""

    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    text: str
    locator: str = Field(min_length=1)
    concept_ids: list[str]
    relevance: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    checksum: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Require the stable evidence identity derived from the chunk ID."""

        if self.evidence_id != evidence_id_for_chunk(self.chunk_id):
            raise DomainError(
                code="EVIDENCE_ID_MISMATCH",
                module="m2",
                message="evidence identity must match its chunk",
            )

    def citation_label(self) -> str:
        """Return a compact deterministic source-and-locator label."""

        return f"{self.source_id}@{self.locator}"


class EvidenceBundle(ContractModel):
    """M2 retrieval output; an empty chunk list is a valid conservative result."""

    query_id: str = Field(min_length=1)
    index_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    course_package_id: str | None = Field(
        default=None,
        exclude_if=_exclude_none,
    )
    course_package_checksum: str | None = Field(
        default=None,
        exclude_if=_exclude_none,
    )
    index_checksum: str | None = Field(
        default=None,
        exclude_if=_exclude_none,
    )
    evidence_chunks: list[EvidenceChunk]
    retrieved_at: datetime

    def validate_business_rules(self) -> None:
        """Require evidence identifiers to be unique within one response."""

        evidence_ids = [chunk.evidence_id for chunk in self.evidence_chunks]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise DomainError(
                code="DUPLICATE_EVIDENCE_ID",
                module="m2",
                message="evidence identifiers must be unique within a bundle",
            )

    def is_empty(self) -> bool:
        """Return whether retrieval produced no governed evidence."""

        return not self.evidence_chunks

    def top(self, n: int) -> list[EvidenceChunk]:
        """Return a stable relevance-descending copy of at most ``n`` chunks."""

        if n < 0:
            raise DomainError(
                code="INVALID_TOP_N",
                module="m2",
                message="top result count must not be negative",
                details={"n": n},
            )
        ordered = sorted(
            self.evidence_chunks,
            key=lambda chunk: chunk.relevance,
            reverse=True,
        )[:n]
        return [chunk.model_copy(deep=True) for chunk in ordered]

    def citation_ids(self) -> list[str]:
        """Return evidence IDs in retrieval order as a new list."""

        return [chunk.evidence_id for chunk in self.evidence_chunks]

    def contains_source(self, source_id: str) -> bool:
        """Return whether any chunk originates from the requested source."""

        return any(chunk.source_id == source_id for chunk in self.evidence_chunks)
