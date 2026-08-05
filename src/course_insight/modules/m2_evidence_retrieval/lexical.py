"""Deterministic NFKC lexical indexing and integer scoring for M2."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import evidence_id_for_chunk
from course_insight.modules.m2_evidence_retrieval.snapshots import (
    LexicalIndexSnapshot,
    _TOKENIZER_VERSION,
    _fold_text,
    _lexical_token_occurrences,
    _lexical_tokens,
    _LexicalDocument,
    semantic_checksum,
    _validate_snapshot,
    snapshot_from_payloads,
    snapshot_to_payloads,
)


TOKENIZER_VERSION = _TOKENIZER_VERSION
_QUANTUM = Decimal("0.000000000001")


@dataclass(frozen=True, slots=True)
class RankedDocument:
    evidence_id: str
    chunk_id: str
    source_id: str
    text: str
    locator: str
    concept_ids: tuple[str, ...]
    text_sha256: str
    relevance: float
    score_points: int

    def __post_init__(self) -> None:
        if type(self.concept_ids) is not tuple:
            raise ValueError("ranked concepts must be a tuple")


def fold_text(value: str) -> str:
    """NFKC/casefold and collapse all Unicode whitespace deterministically."""

    return _fold_text(value)


def lexical_tokens(value: str) -> tuple[str, ...]:
    """Emit unique ASCII runs plus Han unigrams and adjacent-Han bigrams."""

    return _lexical_tokens(value)


def compile_snapshot(course_package: CoursePackage) -> LexicalIndexSnapshot:
    """Compile a ready, checksum-valid M1 package into a canonical snapshot."""

    try:
        if not isinstance(course_package, CoursePackage):
            raise ValueError
        package = CoursePackage.model_validate(
            course_package.model_dump(mode="python", warnings="error")
        )
        if package.status != "ready":
            raise ValueError
        package.validate_business_rules()
        if package.checksum != package.recalculate_checksum():
            raise ValueError
    except Exception:
        raise ValueError("course package is invalid") from None
    course_package = package
    source_ids = [source.source_id for source in course_package.source_documents]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("course package source IDs are duplicated")
    source_id_set = set(source_ids)
    documents: list[_LexicalDocument] = []
    chunk_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for chunk in course_package.content_chunks:
        if chunk.source_id not in source_id_set:
            raise ValueError("chunk source is missing")
        if chunk.chunk_id in chunk_ids:
            raise ValueError("chunk ID is duplicated")
        text_sha256 = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        if text_sha256 != chunk.sha256:
            raise ValueError("chunk text checksum mismatch")
        try:
            evidence_id = evidence_id_for_chunk(chunk.chunk_id)
        except DomainError:
            raise ValueError("course package evidence identity is invalid") from None
        if evidence_id in evidence_ids:
            raise ValueError("evidence ID is duplicated")
        concepts = tuple(sorted(set(chunk.concept_hints)))
        documents.append(
            _LexicalDocument(
                evidence_id=evidence_id,
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                text=chunk.text,
                locator=chunk.locator,
                concept_ids=concepts,
                text_sha256=text_sha256,
                folded_text=fold_text(chunk.text),
                tokens=lexical_tokens(chunk.text),
            )
        )
        chunk_ids.add(chunk.chunk_id)
        evidence_ids.add(evidence_id)
    documents.sort(key=lambda row: (row.chunk_id, row.source_id, row.locator))
    postings_map: dict[str, list[str]] = {}
    for document in documents:
        for token in document.tokens:
            postings_map.setdefault(token, []).append(document.chunk_id)
    snapshot = LexicalIndexSnapshot(
        course_package_id=course_package.course_package_id,
        course_package_checksum=course_package.checksum,
        tokenizer_version=TOKENIZER_VERSION,
        documents=tuple(documents),
        postings=tuple((token, tuple(chunk_ids)) for token, chunk_ids in sorted(postings_map.items())),
        checksum="0" * 64,
    )
    return LexicalIndexSnapshot(
        course_package_id=snapshot.course_package_id,
        course_package_checksum=snapshot.course_package_checksum,
        tokenizer_version=snapshot.tokenizer_version,
        documents=snapshot.documents,
        postings=snapshot.postings,
        checksum=semantic_checksum(snapshot),
    )


def rank_snapshot(
    snapshot: LexicalIndexSnapshot,
    *,
    query_text: str,
    concept_ids: Sequence[str],
) -> tuple[RankedDocument, ...]:
    """Score every indexed document with stable integer lexical mechanics."""

    _validate_snapshot(snapshot)
    if (
        not isinstance(query_text, str)
        or isinstance(concept_ids, str)
        or not isinstance(concept_ids, Sequence)
    ):
        raise ValueError("query inputs are invalid")
    try:
        query_text.encode("utf-8")
        for value in concept_ids:
            if not isinstance(value, str):
                raise ValueError
            value.encode("utf-8")
    except (UnicodeError, ValueError):
        raise ValueError("query inputs are invalid") from None
    query_tokens = lexical_tokens(query_text)
    query_token_set = set(query_tokens)
    folded_phrase = fold_text(query_text)
    phrase_applies = len(_lexical_token_occurrences(query_text)) >= 2 and bool(folded_phrase)
    requested_concepts = set(concept_ids)
    scored: list[tuple[_LexicalDocument, int]] = []
    for document in snapshot.documents:
        points = 100 * len(query_token_set.intersection(document.tokens))
        if phrase_applies and folded_phrase in document.folded_text:
            points += 25
        if requested_concepts.intersection(document.concept_ids):
            points += 50
        scored.append((document, points))
    maximum = max((points for _, points in scored), default=0)
    ranked = [
        RankedDocument(
            evidence_id=document.evidence_id,
            chunk_id=document.chunk_id,
            source_id=document.source_id,
            text=document.text,
            locator=document.locator,
            concept_ids=document.concept_ids,
            text_sha256=document.text_sha256,
            relevance=(
                float((Decimal(points) / Decimal(maximum)).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN))
                if maximum
                else 0.0
            ),
            score_points=points,
        )
        for document, points in scored
    ]
    ranked.sort(key=lambda row: (-row.score_points, row.chunk_id, row.evidence_id))
    return tuple(ranked)


__all__ = [
    "TOKENIZER_VERSION",
    "LexicalIndexSnapshot",
    "RankedDocument",
    "compile_snapshot",
    "fold_text",
    "lexical_tokens",
    "rank_snapshot",
    "snapshot_from_payloads",
    "snapshot_to_payloads",
]
