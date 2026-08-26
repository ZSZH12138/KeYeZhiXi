"""Deterministic batching of bounded M1 chunks for knowledge extraction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge_ingestion import KnowledgeExtractionBatch
from course_insight.modules.m1_course_governance.chunking import (
    ChunkingPolicy,
    visible_character_count,
)


def build_extraction_batches(
    chunks: Sequence[ContentChunk],
    *,
    course_id: str,
    max_chars: int = 6_000,
    max_chunks: int | None = None,
) -> tuple[KnowledgeExtractionBatch, ...]:
    """Group adjacent M1 chunks by text budget while preserving order."""

    ChunkingPolicy(max_chars=max_chars)
    if max_chunks is not None and (
        isinstance(max_chunks, bool)
        or not isinstance(max_chunks, int)
        or not 1 <= max_chunks <= 4_096
    ):
        raise ValueError("max_chunks must be None or an integer between 1 and 4096")
    if not isinstance(course_id, str) or not course_id.strip():
        raise ValueError("course_id must be non-empty")
    if not chunks:
        return ()

    groups: list[list[ContentChunk]] = []
    current: list[ContentChunk] = []
    current_count = 0

    for chunk in chunks:
        count = visible_character_count(chunk.text)
        if count > max_chars:
            raise DomainError(
                code="KNOWLEDGE_CHUNK_TOO_LARGE",
                module="m7",
                message="knowledge chunk must be split by M1 before extraction batching",
                details={"chunk_id": chunk.chunk_id},
            )
        if current and (
            current_count + count > max_chars
            or (max_chunks is not None and len(current) >= max_chunks)
        ):
            groups.append(current)
            current = []
            current_count = 0
        current.append(chunk)
        current_count += count

    if current:
        groups.append(current)

    return tuple(
        KnowledgeExtractionBatch(
            batch_id=_batch_id(course_id, group, max_chars),
            course_id=course_id,
            chunks=list(group),
            max_chars=max_chars,
        )
        for group in groups
    )


def _batch_id(course_id: str, chunks: list[ContentChunk], max_chars: int) -> str:
    payload = json.dumps(
        {
            "course_id": course_id,
            "max_chars": max_chars,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "source_id": chunk.source_id,
                    "sha256": chunk.sha256,
                }
                for chunk in chunks
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"batch_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
