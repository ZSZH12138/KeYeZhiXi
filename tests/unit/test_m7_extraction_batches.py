from __future__ import annotations

import pytest

from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError
from course_insight.modules.m7_local_model.extraction_batches import (
    build_extraction_batches,
)


def _chunk(index: int, text: str) -> ContentChunk:
    return ContentChunk(
        chunk_id=f"chunk-{index}",
        source_id=f"source-{index}",
        text=text,
        locator=f"paragraph:{index}",
        concept_hints=[],
        sha256=f"{index:064x}",
    )


def test_adjacent_chunks_are_grouped_without_exceeding_limit() -> None:
    batches = build_extraction_batches(
        [_chunk(1, "甲" * 400), _chunk(2, "乙" * 500), _chunk(3, "丙" * 300)],
        course_id="course-1",
        max_chars=1_000,
    )

    assert [[chunk.chunk_id for chunk in batch.chunks] for batch in batches] == [
        ["chunk-1", "chunk-2"],
        ["chunk-3"],
    ]
    assert [batch.character_count for batch in batches] == [900, 300]


def test_long_m1_segments_are_not_rejoined_over_limit() -> None:
    batches = build_extraction_batches(
        [_chunk(1, "甲" * 700), _chunk(2, "乙" * 700)],
        course_id="course-1",
        max_chars=1_000,
    )

    assert len(batches) == 2
    assert [batch.character_count for batch in batches] == [700, 700]


def test_default_batches_do_not_split_underfilled_text_at_sixty_four_chunks() -> None:
    batches = build_extraction_batches(
        [_chunk(index, "短段") for index in range(1, 66)],
        course_id="course-1",
    )

    assert [len(batch.chunks) for batch in batches] == [65]


def test_optional_chunk_guard_can_still_bound_pathological_payloads() -> None:
    batches = build_extraction_batches(
        [_chunk(index, "短段") for index in range(1, 66)],
        course_id="course-1",
        max_chunks=64,
    )

    assert [len(batch.chunks) for batch in batches] == [64, 1]


def test_batch_ids_are_deterministic() -> None:
    chunks = [_chunk(1, "甲" * 400), _chunk(2, "乙" * 400)]

    first = build_extraction_batches(chunks, course_id="course-1", max_chars=1_000)
    second = build_extraction_batches(chunks, course_id="course-1", max_chars=1_000)

    assert [batch.batch_id for batch in first] == [batch.batch_id for batch in second]


def test_builder_rejects_one_chunk_above_limit() -> None:
    with pytest.raises(DomainError, match="split by M1"):
        build_extraction_batches(
            [_chunk(1, "甲" * 1_001)],
            course_id="course-1",
            max_chars=1_000,
        )


def test_empty_chunks_produce_no_batches() -> None:
    assert build_extraction_batches([], course_id="course-1", max_chars=1_000) == ()
