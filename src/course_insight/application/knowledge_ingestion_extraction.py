"""Pure extraction helpers shared by the knowledge-ingestion processor."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Callable, Protocol

from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.contracts.knowledge_ingestion import KnowledgeExtractionBatch
from course_insight.modules.m0_platform.django_app.models import (
    CourseSource,
    CourseSourceVersion,
)
from course_insight.modules.m1_course_governance.chunking import (
    ChunkingPolicy,
    split_parsed_blocks,
    visible_character_count,
)
from course_insight.modules.m1_course_governance.legacy_powerpoint import (
    LegacyPowerPointConversionError,
    LegacyPowerPointConverter,
)
from course_insight.modules.m1_course_governance.parsers import ParsedBlock, parse_source
from course_insight.modules.m7_local_model.extraction_batches import (
    build_extraction_batches,
)


_SAFE_DEEPSEEK_ERROR_CODES = frozenset(
    {
        "DEEPSEEK_ACCESS_DENIED",
        "DEEPSEEK_API_KEY_MISSING",
        "DEEPSEEK_AUTH_FAILED",
        "DEEPSEEK_CONTENT_FILTERED",
        "DEEPSEEK_INCOMPLETE_RESPONSE",
        "DEEPSEEK_MODEL_MISMATCH",
        "DEEPSEEK_MODEL_UNCONFIGURED",
        "DEEPSEEK_NETWORK_ERROR",
        "DEEPSEEK_RATE_LIMITED",
        "DEEPSEEK_REQUEST_REJECTED",
        "DEEPSEEK_RESPONSE_INVALID",
        "DEEPSEEK_SERVICE_UNAVAILABLE",
    }
)
_POWERPOINT_MEDIA_TYPES = frozenset(
    {
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)


class ExtractionAdapter(Protocol):
    def extract(
        self,
        batch: KnowledgeExtractionBatch,
        *,
        model_ref: LLMModelRef,
        created_at: datetime,
        on_retry: Callable[[int, int, str], None] | None = None,
    ) -> Any: ...


def _safe_ingestion_error_code(error: Exception) -> str:
    if not isinstance(error, DomainError):
        return "KNOWLEDGE_INGESTION_FAILED"
    nested_code = error.details.get("error_code")
    if nested_code in _SAFE_DEEPSEEK_ERROR_CODES:
        return str(nested_code)
    return error.code


def _safe_source_processing_error_code(error: Exception) -> str:
    if isinstance(error, LegacyPowerPointConversionError):
        return error.code
    return "SOURCE_PROCESSING_FAILED"


def _recoverable_extraction_retry_reason(error: DomainError) -> str | None:
    if error.code == "KNOWLEDGE_EXTRACTION_OUTPUT_INVALID":
        return error.code
    if (
        error.code == "KNOWLEDGE_EXTRACTION_FAILED"
        and error.details.get("error_code") == "DEEPSEEK_INCOMPLETE_RESPONSE"
    ):
        return "DEEPSEEK_INCOMPLETE_RESPONSE"
    return None


def _bounded_phase_progress(
    start: int,
    end: int,
    completed: int,
    total: int,
) -> int:
    if total <= 0:
        return end
    bounded_completed = min(max(completed, 0), total)
    return start + ((end - start) * bounded_completed // total)


def _parse_chunks(
    source: CourseSource,
    version: CourseSourceVersion,
    payload: bytes,
    *,
    legacy_powerpoint_converter: LegacyPowerPointConverter | None = None,
) -> list[ContentChunk]:
    parsed = parse_source(
        source.display_name,
        payload,
        legacy_powerpoint_converter=legacy_powerpoint_converter,
    )
    parsed_blocks = (
        _coalesce_powerpoint_slide_blocks(parsed.blocks)
        if parsed.media_type in _POWERPOINT_MEDIA_TYPES
        else parsed.blocks
    )
    blocks = split_parsed_blocks(parsed_blocks)
    chunks: list[ContentChunk] = []
    for block in blocks:
        text_hash = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
        seed = f"{version.pk}\0{block.locator}\0{text_hash}"
        chunks.append(
            ContentChunk(
                chunk_id=f"chunk_{hashlib.sha256(seed.encode('utf-8')).hexdigest()}",
                source_id=str(version.pk),
                text=block.text,
                locator=block.locator,
                concept_hints=[],
                sha256=text_hash,
            )
        )
    if not chunks:
        raise ValueError("source contains no text chunks")
    return chunks


def _coalesce_powerpoint_slide_blocks(
    blocks: tuple[ParsedBlock, ...],
) -> tuple[ParsedBlock, ...]:
    """Join tiny PowerPoint paragraphs and cells into source-grounded slides."""

    grouped: list[tuple[str, list[str]]] = []
    for block in blocks:
        slide_locator, separator, _ = block.locator.partition(";")
        if not separator or not slide_locator.startswith("slide:"):
            slide_locator = block.locator
        if grouped and grouped[-1][0] == slide_locator:
            grouped[-1][1].append(block.text)
        else:
            grouped.append((slide_locator, [block.text]))
    return tuple(
        ParsedBlock(
            text="\n".join(texts),
            locator=locator,
            ordinal=ordinal,
        )
        for ordinal, (locator, texts) in enumerate(grouped, start=1)
    )


def _bisect_extraction_batch(
    batch: KnowledgeExtractionBatch,
) -> tuple[KnowledgeExtractionBatch, ...]:
    if len(batch.chunks) > 1:
        middle = len(batch.chunks) // 2
        return (
            *build_extraction_batches(
                batch.chunks[:middle],
                course_id=batch.course_id,
                max_chars=batch.max_chars,
            ),
            *build_extraction_batches(
                batch.chunks[middle:],
                course_id=batch.course_id,
                max_chars=batch.max_chars,
            ),
        )
    chunk = batch.chunks[0]
    count = visible_character_count(chunk.text)
    if count <= 1_000:
        raise DomainError(
            code="KNOWLEDGE_EXTRACTION_OVERFLOW",
            module="m7",
            message="knowledge extraction overflow cannot be safely divided further",
        )
    maximum = max(1_000, count // 2)
    blocks = split_parsed_blocks(
        (ParsedBlock(chunk.text, chunk.locator, 1),),
        ChunkingPolicy(max_chars=maximum),
    )
    children = [
        ContentChunk(
            chunk_id=(
                "chunk_"
                + hashlib.sha256(
                    f"{chunk.chunk_id}\0{block.locator}".encode()
                ).hexdigest()
            ),
            source_id=chunk.source_id,
            text=block.text,
            locator=block.locator,
            concept_hints=[],
            sha256=hashlib.sha256(block.text.encode()).hexdigest(),
        )
        for block in blocks
    ]
    return build_extraction_batches(
        children,
        course_id=batch.course_id,
        max_chars=maximum,
    )


__all__ = [
    "ExtractionAdapter",
    "_bisect_extraction_batch",
    "_bounded_phase_progress",
    "_coalesce_powerpoint_slide_blocks",
    "_parse_chunks",
    "_recoverable_extraction_retry_reason",
    "_safe_ingestion_error_code",
    "_safe_source_processing_error_code",
]
