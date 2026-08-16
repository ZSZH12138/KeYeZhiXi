from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from dataclasses import fields, replace
from datetime import datetime, timezone

import pytest

from course_insight.contracts.course import ContentChunk, CoursePackage, SourceAuthorization, SourceDocument
from course_insight.infrastructure.json_io import dumps_json
from course_insight.modules.m2_evidence_retrieval.lexical import (
    TOKENIZER_VERSION,
    LexicalIndexSnapshot,
    compile_snapshot,
    lexical_tokens,
    rank_snapshot,
    snapshot_from_payloads,
    snapshot_to_payloads,
)
from course_insight.modules.m2_evidence_retrieval.snapshots import semantic_checksum
from course_insight.modules.m2_evidence_retrieval.snapshots import _LexicalDocument


def _chunk(
    chunk_id: str,
    text: str = "课程规则",
    *,
    concepts: list[str] | None = None,
) -> ContentChunk:
    return ContentChunk(
        chunk_id=chunk_id,
        source_id="source_alpha",
        text=text,
        locator=f"paragraph:{chunk_id}",
        concept_hints=concepts or [],
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _package(chunks: list[ContentChunk]) -> CoursePackage:
    candidate = CoursePackage(
        course_package_id="course_package_alpha",
        course_id="course_alpha",
        package_version="v1",
        source_documents=[
            SourceDocument(
                source_id="source_alpha",
                file_name="course.md",
                media_type="text/markdown",
                sha256="a" * 64,
                page_count=None,
                title="Course",
                version="v1",
            )
        ],
        content_chunks=chunks,
        source_authorizations=[
            SourceAuthorization(
                source_id="source_alpha",
                authorized_by="teacher",
                license_note="allowed",
                authorized_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
        ],
        imported_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(update={"checksum": candidate.recalculate_checksum()})


def _two_source_package(*, reverse: bool) -> CoursePackage:
    chunks = [
        ContentChunk(
            chunk_id="chunk_a",
            source_id="source_a",
            text="alpha",
            locator="paragraph:1",
            concept_hints=[],
            sha256=hashlib.sha256(b"alpha").hexdigest(),
        ),
        ContentChunk(
            chunk_id="chunk_b",
            source_id="source_b",
            text="beta",
            locator="paragraph:1",
            concept_hints=[],
            sha256=hashlib.sha256(b"beta").hexdigest(),
        ),
    ]
    sources = [
        SourceDocument(source_id="source_a", file_name="a.md", media_type="text/markdown", sha256="a" * 64, page_count=None, title="Course", version="v1"),
        SourceDocument(source_id="source_b", file_name="b.md", media_type="text/markdown", sha256="b" * 64, page_count=None, title="Course", version="v1"),
    ]
    authorizations = [
        SourceAuthorization(source_id="source_a", authorized_by="teacher", license_note="allowed", authorized_at=datetime(2026, 8, 3, tzinfo=timezone.utc)),
        SourceAuthorization(source_id="source_b", authorized_by="teacher", license_note="allowed", authorized_at=datetime(2026, 8, 3, tzinfo=timezone.utc)),
    ]
    if reverse:
        chunks.reverse()
        sources.reverse()
        authorizations.reverse()
    candidate = CoursePackage(
        course_package_id="course_package_alpha",
        course_id="course_alpha",
        package_version="v1",
        source_documents=sources,
        content_chunks=chunks,
        source_authorizations=authorizations,
        imported_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(update={"checksum": candidate.recalculate_checksum()})


def test_tokenize_nfkc_emits_ascii_han_unigrams_and_adjacent_bigrams() -> None:
    assert lexical_tokens("ＡＢＣ：x 课程知识") == (
        "abc:x",
        "课",
        "程",
        "知",
        "识",
        "课程",
        "程知",
        "知识",
    )


def test_tokenize_punctuation_and_whitespace_break_han_bigrams() -> None:
    assert lexical_tokens("课，程 知\t识") == ("课", "程", "知", "识")


def test_tokenize_removes_duplicate_tokens_and_ignores_non_matching_text() -> None:
    assert lexical_tokens("课 a a !!!") == ("课", "a")
    assert lexical_tokens(" \u3000!!!\n") == ()


def test_compile_snapshot_sorts_documents_and_postings_not_python_input_order() -> None:
    first = compile_snapshot(_package([_chunk("chunk_b"), _chunk("chunk_a")]))
    second = compile_snapshot(_package([_chunk("chunk_a"), _chunk("chunk_b")]))
    assert first.documents == second.documents
    assert first.postings == second.postings


def test_compile_snapshot_rejects_non_ready_rechecksum_and_bad_chunk_source() -> None:
    package = _package([_chunk("chunk_a")])
    with pytest.raises(ValueError):
        compile_snapshot(package.model_copy(update={"status": "draft"}))
    with pytest.raises(ValueError):
        compile_snapshot(package.model_copy(update={"checksum": "0" * 64}))
    bad = _chunk("chunk_bad").model_copy(update={"source_id": "missing"})
    with pytest.raises(ValueError):
        compile_snapshot(package.model_copy(update={"content_chunks": [bad]}))


def test_snapshot_payload_round_trip_is_canonical_and_rejects_tampering() -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_a", "课程规则", concepts=["c2", "c1", "c1"])]))
    payloads = snapshot_to_payloads(snapshot)
    assert tuple(payloads) == ("documents.json", "postings.json")
    assert snapshot_from_payloads(payloads) == snapshot
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "extra.json": b"{}"})
    malformed = dict(payloads)
    malformed["documents.json"] = b"{bad"
    with pytest.raises(ValueError):
        snapshot_from_payloads(malformed)
    altered = dict(payloads)
    altered["postings.json"] = altered["postings.json"].replace(
        TOKENIZER_VERSION.encode(), b"m2_bad_tokenizer_v1"
    )
    with pytest.raises(ValueError):
        snapshot_from_payloads(altered)


def test_snapshot_payload_rejects_noncanonical_extra_duplicate_and_bad_postings() -> None:
    payloads = snapshot_to_payloads(compile_snapshot(_package([_chunk("chunk_a", "课程")])))
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "documents.json": b"{\n  \"x\": 1\n}"})
    with pytest.raises(ValueError):
        snapshot_from_payloads(
            {**payloads, "documents.json": b'{"format_version":1,"format_version":1}'}
        )

    documents = json.loads(payloads["documents.json"])
    documents["unexpected"] = True
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "documents.json": dumps_json(documents).encode("utf-8")})

    duplicate = json.loads(payloads["documents.json"])
    duplicate["documents"].append(duplicate["documents"][0])
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "documents.json": dumps_json(duplicate).encode("utf-8")})

    postings = json.loads(payloads["postings.json"])
    postings["postings"] = postings["postings"][:-1]
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "postings.json": dumps_json(postings).encode("utf-8")})


def test_snapshot_payload_rejects_missing_keys_and_checksum_forgery() -> None:
    payloads = snapshot_to_payloads(compile_snapshot(_package([_chunk("chunk_a")])))
    documents = json.loads(payloads["documents.json"])
    del documents["documents"]
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "documents.json": dumps_json(documents).encode("utf-8")})
    forged = json.loads(payloads["postings.json"])
    forged["index_checksum"] = "0" * 64
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "postings.json": dumps_json(forged).encode("utf-8")})


def test_snapshot_payload_rejects_wrong_format_and_non_sorted_postings() -> None:
    payloads = snapshot_to_payloads(compile_snapshot(_package([_chunk("chunk_a", "课程 a")])))
    documents = json.loads(payloads["documents.json"])
    documents["format_version"] = 2
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "documents.json": dumps_json(documents).encode("utf-8")})
    postings = json.loads(payloads["postings.json"])
    postings["postings"].reverse()
    with pytest.raises(ValueError):
        snapshot_from_payloads({**payloads, "postings.json": dumps_json(postings).encode("utf-8")})


@pytest.mark.parametrize("format_version", [True, 1.0])
def test_snapshot_payload_requires_an_exact_integer_format_version(format_version: object) -> None:
    payloads = snapshot_to_payloads(compile_snapshot(_package([_chunk("chunk_a")])))
    documents = json.loads(payloads["documents.json"])
    postings = json.loads(payloads["postings.json"])
    documents["format_version"] = format_version
    postings["format_version"] = format_version
    with pytest.raises(ValueError):
        snapshot_from_payloads(
            {
                "documents.json": dumps_json(documents).encode("utf-8"),
                "postings.json": dumps_json(postings).encode("utf-8"),
            }
        )


def test_rank_is_integer_scored_phrase_and_concept_stable() -> None:
    snapshot = compile_snapshot(
        _package(
            [
                _chunk("chunk_a", "课程 规则", concepts=["c1"]),
                _chunk("chunk_b", "课 程", concepts=[]),
            ]
        )
    )
    ranked = rank_snapshot(snapshot, query_text="课程 规则", concept_ids=["c1", "c1"])
    assert [(row.chunk_id, row.score_points, row.relevance) for row in ranked] == [
        ("chunk_a", 675, 1.0),
        ("chunk_b", 200, pytest.approx(200 / 675, abs=1e-12)),
    ]


def test_rank_denominator_includes_documents_that_callers_may_filter() -> None:
    snapshot = compile_snapshot(
        _package([_chunk("chunk_a", "课程 规则"), _chunk("chunk_b", "课 程")])
    )
    ranked = rank_snapshot(snapshot, query_text="课程 规则", concept_ids=[])
    assert ranked[1].relevance == pytest.approx(200 / 625, abs=1e-12)


def test_rank_quantizes_public_relevance_to_twelve_decimal_places() -> None:
    snapshot = compile_snapshot(
        _package(
            [
                _chunk("chunk_a", "课 程"),
                _chunk("chunk_b", "课", concepts=["c1"]),
            ]
        )
    )
    ranked = rank_snapshot(snapshot, query_text="课 程", concept_ids=["c1"])
    assert ranked[1].score_points == 150
    assert ranked[1].relevance == 0.666666666667


def test_rank_phrase_uses_pre_dedup_token_occurrences_and_single_token_has_no_boost() -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_a", "alpha alpha")]))
    repeated = rank_snapshot(snapshot, query_text="alpha alpha", concept_ids=[])
    single = rank_snapshot(snapshot, query_text="alpha", concept_ids=[])
    assert repeated[0].score_points == 125
    assert single[0].score_points == 100


def test_compile_reruns_business_rules_and_maps_invalid_ready_model_copy_to_value_error() -> None:
    package = _package([_chunk("chunk_a")])
    invalid = package.model_copy(update={"source_documents": [], "content_chunks": []})
    invalid = invalid.model_copy(update={"checksum": invalid.recalculate_checksum()})
    with pytest.raises(ValueError):
        compile_snapshot(invalid)


def test_rank_zero_scores_are_all_zero_and_ties_are_chunk_then_evidence_stable() -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_b", "x"), _chunk("chunk_a", "x")]))
    ranked = rank_snapshot(snapshot, query_text="not-found", concept_ids=[])
    assert [(row.chunk_id, row.relevance) for row in ranked] == [
        ("chunk_a", 0.0),
        ("chunk_b", 0.0),
    ]


def _payloads_with_recomputed_checksum(
    payloads: dict[str, bytes],
    *,
    document_update: dict[str, object],
) -> dict[str, bytes]:
    documents = json.loads(payloads["documents.json"])
    postings = json.loads(payloads["postings.json"])
    documents["documents"][0].update(document_update)
    documents_core = {key: value for key, value in documents.items() if key != "index_checksum"}
    postings_core = {key: value for key, value in postings.items() if key != "index_checksum"}
    checksum = hashlib.sha256(
        dumps_json(
            {
                "course_package_checksum": documents["course_package_checksum"],
                "course_package_id": documents["course_package_id"],
                "documents_sha256": hashlib.sha256(dumps_json(documents_core).encode("utf-8")).hexdigest(),
                "format_version": 1,
                "postings_sha256": hashlib.sha256(dumps_json(postings_core).encode("utf-8")).hexdigest(),
                "tokenizer_version": TOKENIZER_VERSION,
            }
        ).encode("utf-8")
    ).hexdigest()
    documents["index_checksum"] = checksum
    postings["index_checksum"] = checksum
    return {
        "documents.json": dumps_json(documents).encode("utf-8"),
        "postings.json": dumps_json(postings).encode("utf-8"),
    }


@pytest.mark.parametrize("document_update", [{"folded_text": "forged"}, {"tokens": ["forged"]}])
def test_snapshot_restore_rejects_forged_document_semantics_even_with_recomputed_checksum(
    document_update: dict[str, object],
) -> None:
    payloads = snapshot_to_payloads(compile_snapshot(_package([_chunk("chunk_a", "课程")])))
    with pytest.raises(ValueError):
        snapshot_from_payloads(
            _payloads_with_recomputed_checksum(payloads, document_update=document_update)
        )


def test_snapshot_has_no_time_or_host_path_fields() -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_a")]))
    assert {field.name for field in fields(snapshot)}.isdisjoint({"built_at", "path", "absolute_path"})
    payload = b"".join(snapshot_to_payloads(snapshot).values()).decode("utf-8")
    assert "built_at" not in payload
    assert "C:\\\\" not in payload


def test_reordered_valid_packages_keep_canonical_documents_but_distinct_checksum_binding() -> None:
    first = compile_snapshot(_two_source_package(reverse=False))
    second = compile_snapshot(_two_source_package(reverse=True))
    assert first.documents == second.documents
    assert first.postings == second.postings
    assert first.course_package_checksum != second.course_package_checksum
    assert first.checksum != second.checksum


def test_semantic_checksum_includes_the_course_package_checksum_binding() -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_a")]))
    rebound = replace(snapshot, course_package_checksum="f" * 64, checksum="0" * 64)
    rebound = replace(rebound, checksum=semantic_checksum(rebound))
    assert rebound.documents == snapshot.documents
    assert rebound.postings == snapshot.postings
    assert rebound.checksum != snapshot.checksum


def _self_consistent_empty_snapshot() -> LexicalIndexSnapshot:
    empty = LexicalIndexSnapshot(
        course_package_id="course_package_alpha",
        course_package_checksum="a" * 64,
        tokenizer_version=TOKENIZER_VERSION,
        documents=(),
        postings=(),
        checksum="0" * 64,
    )
    return replace(empty, checksum=semantic_checksum(empty))


def test_snapshot_to_payloads_rejects_a_self_consistent_empty_snapshot() -> None:
    with pytest.raises(ValueError):
        snapshot_to_payloads(_self_consistent_empty_snapshot())


def test_snapshot_from_payloads_rejects_a_self_consistent_empty_snapshot() -> None:
    empty = _self_consistent_empty_snapshot()
    payloads = {
        "documents.json": dumps_json(
            {
                "course_package_checksum": empty.course_package_checksum,
                "course_package_id": empty.course_package_id,
                "documents": [],
                "format_version": 1,
                "index_checksum": empty.checksum,
                "tokenizer_version": TOKENIZER_VERSION,
            }
        ).encode("utf-8"),
        "postings.json": dumps_json(
            {
                "course_package_checksum": empty.course_package_checksum,
                "course_package_id": empty.course_package_id,
                "format_version": 1,
                "index_checksum": empty.checksum,
                "postings": [],
                "tokenizer_version": TOKENIZER_VERSION,
            }
        ).encode("utf-8"),
    }
    with pytest.raises(ValueError):
        snapshot_from_payloads(payloads)


def test_private_snapshot_rows_reject_mutable_tuple_fields_and_cannot_be_appended() -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_a")]))
    document = snapshot.documents[0]
    with pytest.raises(ValueError):
        _LexicalDocument(
            evidence_id=document.evidence_id,
            chunk_id=document.chunk_id,
            source_id=document.source_id,
            text=document.text,
            locator=document.locator,
            concept_ids=[],  # type: ignore[arg-type]
            text_sha256=document.text_sha256,
            folded_text=document.folded_text,
            tokens=document.tokens,
        )
    with pytest.raises(ValueError):
        replace(document, tokens=["mutable"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replace(snapshot, documents=[document])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replace(snapshot, postings=(("course", ["chunk_a"]),))  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        snapshot.documents.append(document)  # type: ignore[attr-defined]


def test_ranked_document_rejects_mutable_concept_ids() -> None:
    from course_insight.modules.m2_evidence_retrieval.lexical import RankedDocument

    with pytest.raises(ValueError):
        RankedDocument(
            evidence_id="evidence_chunk_a",
            chunk_id="chunk_a",
            source_id="source_a",
            text="text",
            locator="paragraph:1",
            concept_ids=[],  # type: ignore[arg-type]
            text_sha256="a" * 64,
            relevance=0.0,
            score_points=0,
        )


@pytest.mark.parametrize(
    "invalid_package",
    [
        lambda package: object(),
        lambda package: package.model_copy(update={"source_documents": [object()]}),
        lambda package: package.model_copy(update={"content_chunks": [object()]}),
        lambda package: package.model_copy(update={"course_id": "\ud800sensitive"}),
        lambda package: package.model_copy(update={"content_chunks": [package.content_chunks[0].model_copy(update={"source_id": "\ud800sensitive"})]}),
        lambda package: package.model_copy(update={"content_chunks": [package.content_chunks[0].model_copy(update={"locator": "\ud800sensitive"})]}),
        lambda package: package.model_copy(update={"content_chunks": [package.content_chunks[0].model_copy(update={"text": "\ud800sensitive"})]}),
        lambda package: package.model_copy(update={"content_chunks": [package.content_chunks[0].model_copy(update={"concept_hints": ["\ud800sensitive"]})]}),
    ],
)
def test_compile_rejects_unvalidated_or_non_utf8_inputs_without_leaking_values(invalid_package: object) -> None:
    package = _package([_chunk("chunk_a")])
    candidate = invalid_package(package)  # type: ignore[operator]
    with pytest.raises(ValueError) as captured:
        compile_snapshot(candidate)  # type: ignore[arg-type]
    assert "sensitive" not in str(captured.value)
    assert "position" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("concept_ids", [None, object(), "concept", ["\ud800sensitive"]])
def test_rank_rejects_invalid_or_non_utf8_query_inputs_without_leaking_values(concept_ids: object) -> None:
    snapshot = compile_snapshot(_package([_chunk("chunk_a")]))
    with pytest.raises(ValueError) as captured:
        rank_snapshot(snapshot, query_text="\ud800sensitive", concept_ids=concept_ids)  # type: ignore[arg-type]
    assert "sensitive" not in str(captured.value)
    assert "position" not in str(captured.value)
    assert captured.value.__cause__ is None


def _nul_identity_snapshot() -> LexicalIndexSnapshot:
    snapshot = compile_snapshot(_package([_chunk("chunk_a")]))
    document = replace(
        snapshot.documents[0],
        chunk_id="chunk_\x00sensitive",
        evidence_id="evidence_chunk_\x00sensitive",
    )
    candidate = replace(snapshot, documents=(document,), checksum="0" * 64)
    return replace(candidate, checksum=semantic_checksum(candidate))


def test_compile_nul_chunk_identity_reaches_evidence_construction_and_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import course_insight.modules.m2_evidence_retrieval.lexical as lexical_module

    package = _package([_chunk("chunk_a")])
    invalid = package.model_copy(
        update={"content_chunks": [_chunk("chunk_a").model_copy(update={"chunk_id": "chunk_\x00sensitive"})]}
    )
    invalid = invalid.model_copy(update={"checksum": invalid.recalculate_checksum()})
    called = False
    original = lexical_module.evidence_id_for_chunk

    def observed(chunk_id: str) -> str:
        nonlocal called
        called = True
        return original(chunk_id)

    monkeypatch.setattr(lexical_module, "evidence_id_for_chunk", observed)
    with pytest.raises(ValueError) as captured:
        compile_snapshot(invalid)
    assert called
    assert str(captured.value) == "course package evidence identity is invalid"
    assert "sensitive" not in str(captured.value)
    assert "position" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "operation",
    [
        lambda: snapshot_to_payloads(_nul_identity_snapshot()),
        lambda: rank_snapshot(_nul_identity_snapshot(), query_text="query", concept_ids=[]),
    ],
)
def test_restored_nul_chunk_identity_is_mapped_to_stable_m2_value_error(operation: object) -> None:
    with pytest.raises(ValueError) as captured:
        operation()  # type: ignore[operator]
    assert "sensitive" not in str(captured.value)
    assert "position" not in str(captured.value)
    assert captured.value.__cause__ is None
