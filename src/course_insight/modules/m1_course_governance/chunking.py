"""Deterministic, source-preserving text chunking for M1 ingestion."""

from __future__ import annotations

from dataclasses import dataclass
import re

from course_insight.modules.m1_course_governance.parsers import ParsedBlock


_PARAGRAPH_BOUNDARY_RE = re.compile(r"\r?\n[ \t]*\r?\n")
_SENTENCE_ENDINGS = frozenset("。！？!?；;")
_WEAK_BOUNDARIES = frozenset("，,、：:\n\r")


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """Bounds for safe extraction requests.

    The limit counts non-whitespace Unicode characters. This keeps the policy
    deterministic across Windows and Unix line endings while preventing a file
    containing only formatting whitespace from consuming an extraction batch.
    """

    max_chars: int = 6_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_chars, bool)
            or not isinstance(self.max_chars, int)
            or not 1_000 <= self.max_chars <= 20_000
        ):
            raise ValueError("max_chars must be an integer between 1000 and 20000")


DEFAULT_CHUNKING_POLICY = ChunkingPolicy()


def visible_character_count(text: str) -> int:
    """Return the number of non-whitespace Unicode characters in ``text``."""

    return sum(not character.isspace() for character in text)


def split_parsed_blocks(
    blocks: tuple[ParsedBlock, ...],
    policy: ChunkingPolicy = DEFAULT_CHUNKING_POLICY,
) -> tuple[ParsedBlock, ...]:
    """Split long parser blocks without losing their original source locator.

    Paragraph boundaries are preferred. A paragraph that is still too long is
    split after sentence punctuation, then weaker punctuation/whitespace, and
    finally by the configured visible-character limit. Character offsets are
    zero-based half-open offsets into the original block text.
    """

    output: list[ParsedBlock] = []
    next_ordinal = 1

    for block in blocks:
        spans = _split_text(block.text, policy.max_chars)
        if spans == ((0, len(block.text)),):
            output.append(
                ParsedBlock(
                    text=block.text,
                    locator=block.locator,
                    ordinal=next_ordinal,
                )
            )
            next_ordinal += 1
            continue

        for segment_number, (start, end) in enumerate(spans, start=1):
            output.append(
                ParsedBlock(
                    text=block.text[start:end],
                    locator=(
                        f"{block.locator};segment:{segment_number};"
                        f"chars:{start}-{end}"
                    ),
                    ordinal=next_ordinal,
                )
            )
            next_ordinal += 1

    return tuple(output)


def _split_text(text: str, maximum: int) -> tuple[tuple[int, int], ...]:
    if visible_character_count(text) <= maximum:
        return ((0, len(text)),)

    spans: list[tuple[int, int]] = []
    cursor = 0
    text_length = len(text)

    while cursor < text_length:
        start = _skip_whitespace(text, cursor)
        if start >= text_length:
            break

        hard_end = _position_after_visible_characters(text, start, maximum)
        if hard_end >= text_length:
            end = text_length
        else:
            end = _preferred_boundary(text, start, hard_end)

        trimmed_start, trimmed_end = _trim_span(text, start, end)
        if trimmed_start < trimmed_end:
            spans.append((trimmed_start, trimmed_end))

        # ``end`` is always greater than ``start``. Skipping separators here
        # means the next locator points at the first character actually sent to
        # extraction, while the offsets still refer to the untouched source.
        cursor = max(end, start + 1)

    return tuple(spans)


def _position_after_visible_characters(text: str, start: int, maximum: int) -> int:
    visible = 0
    for index in range(start, len(text)):
        if not text[index].isspace():
            visible += 1
        if visible == maximum:
            return index + 1
    return len(text)


def _preferred_boundary(text: str, start: int, hard_end: int) -> int:
    paragraph_candidates = [
        match.start()
        for match in _PARAGRAPH_BOUNDARY_RE.finditer(text, start, hard_end)
        if match.start() > start
    ]
    if paragraph_candidates:
        return paragraph_candidates[-1]

    sentence_candidates = [
        index + 1
        for index in range(start, hard_end)
        if text[index] in _SENTENCE_ENDINGS
    ]
    if sentence_candidates:
        return sentence_candidates[-1]

    weak_candidates = [
        index + 1
        for index in range(start, hard_end)
        if text[index] in _WEAK_BOUNDARIES
    ]
    if weak_candidates:
        return weak_candidates[-1]

    whitespace_candidates = [
        index
        for index in range(start + 1, hard_end)
        if text[index].isspace()
    ]
    if whitespace_candidates:
        return whitespace_candidates[-1]

    return hard_end


def _skip_whitespace(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
