from __future__ import annotations

import pytest

from course_insight.modules.m1_course_governance.chunking import (
    ChunkingPolicy,
    split_parsed_blocks,
    visible_character_count,
)
from course_insight.modules.m1_course_governance.parsers import ParsedBlock


def _block(text: str, *, locator: str = "page:1;block:1") -> ParsedBlock:
    return ParsedBlock(text=text, locator=locator, ordinal=1)


def test_short_paragraph_remains_one_unchanged_block() -> None:
    original = _block("拥塞控制用于避免网络过载。")

    result = split_parsed_blocks((original,), ChunkingPolicy(max_chars=1000))

    assert result == (original,)


def test_long_text_prefers_paragraph_boundary() -> None:
    first = "甲" * 600
    second = "乙" * 600

    result = split_parsed_blocks(
        (_block(first + "\n\n" + second),),
        ChunkingPolicy(max_chars=1000),
    )

    assert [item.text for item in result] == [first, second]
    assert [item.ordinal for item in result] == [1, 2]
    assert result[0].locator.endswith("segment:1;chars:0-600")
    assert result[1].locator.endswith("segment:2;chars:602-1202")


def test_single_long_paragraph_prefers_complete_sentences() -> None:
    first_sentence = "慢启动阶段窗口逐步增长，" + "甲" * 570 + "。"
    second_sentence = "发生拥塞后窗口需要调整，" + "乙" * 570 + "。"

    result = split_parsed_blocks(
        (_block(first_sentence + second_sentence),),
        ChunkingPolicy(max_chars=1000),
    )

    assert [item.text for item in result] == [first_sentence, second_sentence]
    assert all(visible_character_count(item.text) <= 1000 for item in result)


def test_punctuation_free_paragraph_uses_visible_character_fallback() -> None:
    text = "网" * 1200

    result = split_parsed_blocks(
        (_block(text),),
        ChunkingPolicy(max_chars=1000),
    )

    assert [visible_character_count(item.text) for item in result] == [1000, 200]
    assert "".join(item.text for item in result) == text
    assert [item.locator for item in result] == [
        "page:1;block:1;segment:1;chars:0-1000",
        "page:1;block:1;segment:2;chars:1000-1200",
    ]


def test_multiple_input_blocks_keep_contiguous_output_ordinals() -> None:
    blocks = (
        ParsedBlock(text="短段落", locator="paragraph:1", ordinal=1),
        ParsedBlock(text="长" * 1200, locator="paragraph:2", ordinal=2),
    )

    result = split_parsed_blocks(blocks, ChunkingPolicy(max_chars=1000))

    assert [item.ordinal for item in result] == [1, 2, 3]
    assert [item.locator for item in result] == [
        "paragraph:1",
        "paragraph:2;segment:1;chars:0-1000",
        "paragraph:2;segment:2;chars:1000-1200",
    ]


@pytest.mark.parametrize("maximum", [999, 20_001, True])
def test_chunking_policy_rejects_unsafe_thresholds(maximum: object) -> None:
    with pytest.raises(ValueError, match="between 1000 and 20000"):
        ChunkingPolicy(max_chars=maximum)  # type: ignore[arg-type]
