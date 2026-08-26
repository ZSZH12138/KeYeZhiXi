"""Contracts for traceable knowledge extraction and release preparation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from course_insight.contracts.base import ContractModel
from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError


EvidenceRelationType = Literal[
    "definition",
    "explanation",
    "example",
    "application",
    "prerequisite",
    "mention",
]


class KnowledgeEvidenceRef(ContractModel):
    """One exact source span supporting a candidate or merged concept."""

    source_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=1)
    relation_type: EvidenceRelationType

    def validate_business_rules(self) -> None:
        if self.span_end <= self.span_start:
            raise DomainError(
                code="KNOWLEDGE_EVIDENCE_SPAN_INVALID",
                module="m3",
                message="knowledge evidence uses an invalid half-open span",
            )

    def identity(self) -> tuple[str, str, int, int, str]:
        """Return the immutable identity used for provenance set union."""

        return (
            self.source_version_id,
            self.chunk_id,
            self.span_start,
            self.span_end,
            self.relation_type,
        )


class KnowledgeCandidate(ContractModel):
    """One of zero or more concepts extracted from a bounded text batch."""

    candidate_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    aliases: list[str]
    evidence: list[KnowledgeEvidenceRef] = Field(min_length=1)

    def validate_business_rules(self) -> None:
        identities = [reference.identity() for reference in self.evidence]
        if len(identities) != len(set(identities)):
            raise DomainError(
                code="KNOWLEDGE_EVIDENCE_DUPLICATED",
                module="m3",
                message="knowledge evidence references must be unique",
            )
        normalized_aliases = [" ".join(alias.split()).casefold() for alias in self.aliases]
        if any(not alias for alias in normalized_aliases) or len(normalized_aliases) != len(
            set(normalized_aliases)
        ):
            raise DomainError(
                code="KNOWLEDGE_ALIAS_INVALID",
                module="m3",
                message="knowledge aliases must be non-empty and unique",
            )


class KnowledgeExtractionBatch(ContractModel):
    """Deterministically grouped M1 chunks for one extraction request."""

    batch_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    chunks: list[ContentChunk] = Field(min_length=1)
    max_chars: int = 6_000

    @field_validator("max_chars", mode="before")
    @classmethod
    def _validate_max_chars(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or not 1_000 <= value <= 20_000:
            raise ValueError("max_chars must be an integer between 1000 and 20000")
        return value

    @property
    def character_count(self) -> int:
        return sum(
            sum(not character.isspace() for character in chunk.text)
            for chunk in self.chunks
        )

    def validate_business_rules(self) -> None:
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise DomainError(
                code="KNOWLEDGE_BATCH_CHUNK_DUPLICATED",
                module="m7",
                message="extraction batch chunk identifiers must be unique",
            )
        if self.character_count > self.max_chars:
            raise DomainError(
                code="KNOWLEDGE_BATCH_TOO_LARGE",
                module="m7",
                message="extraction batch exceeds the configured character limit",
                details={
                    "character_count": self.character_count,
                    "max_chars": self.max_chars,
                },
            )


class KnowledgeExtractionResult(ContractModel):
    """Strict structured extraction result bound to its original batch."""

    result_id: str = Field(min_length=1)
    batch: KnowledgeExtractionBatch
    candidates: list[KnowledgeCandidate] = Field(max_length=100)
    status: Literal["succeeded", "failed"]
    possibly_truncated: bool
    created_at: datetime

    def validate_business_rules(self) -> None:
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise DomainError(
                code="KNOWLEDGE_CANDIDATE_DUPLICATED",
                module="m7",
                message="knowledge candidate identifiers must be unique",
            )
        if self.status == "failed" and (self.candidates or self.possibly_truncated):
            raise DomainError(
                code="KNOWLEDGE_EXTRACTION_RESULT_INVALID",
                module="m7",
                message="failed extraction cannot contain knowledge candidates",
            )

        chunks = {chunk.chunk_id: chunk for chunk in self.batch.chunks}
        for candidate in self.candidates:
            for reference in candidate.evidence:
                chunk = chunks.get(reference.chunk_id)
                if chunk is None:
                    raise DomainError(
                        code="KNOWLEDGE_EVIDENCE_OUTSIDE_BATCH",
                        module="m7",
                        message="knowledge evidence refers outside the extraction batch",
                    )
                if (
                    reference.source_id != chunk.source_id
                    or reference.source_version_id != chunk.source_id
                    or reference.locator != chunk.locator
                ):
                    raise DomainError(
                        code="KNOWLEDGE_EVIDENCE_SOURCE_MISMATCH",
                        module="m7",
                        message="knowledge evidence source does not match its batch chunk",
                    )
                if reference.span_end > len(chunk.text):
                    raise DomainError(
                        code="KNOWLEDGE_EVIDENCE_SPAN_OUTSIDE_CHUNK",
                        module="m7",
                        message="knowledge evidence span is outside its batch chunk",
                    )


class MergedKnowledgeConcept(ContractModel):
    """Canonical course concept with the full union of supporting sources."""

    concept_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    aliases: list[str]
    evidence: list[KnowledgeEvidenceRef] = Field(min_length=1)

    def validate_business_rules(self) -> None:
        identities = [reference.identity() for reference in self.evidence]
        if len(identities) != len(set(identities)):
            raise DomainError(
                code="MERGED_KNOWLEDGE_EVIDENCE_DUPLICATED",
                module="m3",
                message="merged knowledge evidence references must be unique",
            )


class QuestionConceptLinkCandidate(ContractModel):
    """Locally grounded multi-label relation from one question to one concept."""

    question_id: str = Field(min_length=1)
    concept_id: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    status: Literal["usable", "needs_review"]
    evidence: list[KnowledgeEvidenceRef] = Field(min_length=1)

    def validate_business_rules(self) -> None:
        identities = [reference.identity() for reference in self.evidence]
        if len(identities) != len(set(identities)):
            raise DomainError(
                code="QUESTION_LINK_EVIDENCE_DUPLICATED",
                module="m7",
                message="question concept link evidence must be unique",
            )
