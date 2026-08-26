from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge_ingestion import (
    KnowledgeCandidate,
    KnowledgeEvidenceRef,
    KnowledgeExtractionBatch,
    KnowledgeExtractionResult,
)


NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _chunk(chunk_id: str = "chunk-1", text: str = "拥塞控制避免网络过载。") -> ContentChunk:
    return ContentChunk(
        chunk_id=chunk_id,
        source_id="source-version-1",
        text=text,
        locator="page:1;block:1",
        concept_hints=[],
        sha256="a" * 64,
    )


def _batch(*chunks: ContentChunk, max_chars: int = 6_000) -> KnowledgeExtractionBatch:
    return KnowledgeExtractionBatch(
        batch_id="batch-1",
        course_id="course-1",
        chunks=list(chunks or (_chunk(),)),
        max_chars=max_chars,
    )


def _evidence(**overrides: object) -> KnowledgeEvidenceRef:
    payload: dict[str, object] = {
        "source_id": "source-version-1",
        "source_version_id": "source-version-1",
        "chunk_id": "chunk-1",
        "locator": "page:1;block:1",
        "span_start": 0,
        "span_end": 4,
        "relation_type": "definition",
    }
    payload.update(overrides)
    return KnowledgeEvidenceRef.model_validate(payload)


def _candidate(candidate_id: str, name: str, evidence: list[KnowledgeEvidenceRef]) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=candidate_id,
        name=name,
        description=f"{name}的课程内定义",
        aliases=[],
        evidence=evidence,
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 1), (2, 2), (3, 2)],
)
def test_evidence_rejects_invalid_half_open_span(start: int, end: int) -> None:
    with pytest.raises((ValueError, DomainError)):
        _evidence(span_start=start, span_end=end)


def test_candidate_rejects_duplicate_provenance_identity() -> None:
    evidence = _evidence()

    with pytest.raises(DomainError, match="evidence references must be unique"):
        _candidate("candidate-1", "拥塞控制", [evidence, evidence])


def test_batch_rejects_content_over_configured_limit() -> None:
    with pytest.raises(DomainError, match="exceeds"):
        _batch(_chunk(text="网" * 1_001), max_chars=1_000)


def test_result_rejects_evidence_chunk_outside_batch() -> None:
    candidate = _candidate(
        "candidate-1",
        "拥塞控制",
        [_evidence(chunk_id="unknown-chunk")],
    )

    with pytest.raises(DomainError, match="outside the extraction batch"):
        KnowledgeExtractionResult(
            result_id="result-1",
            batch=_batch(),
            candidates=[candidate],
            status="succeeded",
            possibly_truncated=False,
            created_at=NOW,
        )


def test_result_rejects_span_beyond_referenced_chunk() -> None:
    candidate = _candidate(
        "candidate-1",
        "拥塞控制",
        [_evidence(span_end=100)],
    )

    with pytest.raises(DomainError, match="span is outside"):
        KnowledgeExtractionResult(
            result_id="result-1",
            batch=_batch(),
            candidates=[candidate],
            status="succeeded",
            possibly_truncated=False,
            created_at=NOW,
        )


def test_one_batch_accepts_all_returned_concepts() -> None:
    result = KnowledgeExtractionResult(
        result_id="result-1",
        batch=_batch(),
        candidates=[
            _candidate("candidate-1", "拥塞控制", [_evidence()]),
            _candidate("candidate-2", "慢启动", [_evidence(relation_type="example")]),
        ],
        status="succeeded",
        possibly_truncated=False,
        created_at=NOW,
    )

    assert [candidate.name for candidate in result.candidates] == ["拥塞控制", "慢启动"]


def test_empty_successful_result_is_valid() -> None:
    result = KnowledgeExtractionResult(
        result_id="result-empty",
        batch=_batch(),
        candidates=[],
        status="succeeded",
        possibly_truncated=False,
        created_at=NOW,
    )

    assert result.candidates == []
