"""Private canonical snapshot primitives for the M2 lexical index."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from course_insight.infrastructure.json_io import dumps_json
from course_insight.contracts.errors import DomainError


_FORMAT_VERSION = 1
_DOCUMENTS_PAYLOAD = "documents.json"
_POSTINGS_PAYLOAD = "postings.json"
_TOKENIZER_VERSION = "m2_lexical_nfkc_han_v1"
_ASCII_RUN = re.compile(r"[a-z0-9_:+-]+")


@dataclass(frozen=True, slots=True)
class _LexicalDocument:
    evidence_id: str
    chunk_id: str
    source_id: str
    text: str
    locator: str
    concept_ids: tuple[str, ...]
    text_sha256: str
    folded_text: str
    tokens: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(type(self.concept_ids) is tuple, "document concepts must be a tuple")
        _require(type(self.tokens) is tuple, "document tokens must be a tuple")


@dataclass(frozen=True, slots=True)
class LexicalIndexSnapshot:
    """Validated immutable, process-local representation of one M2 index."""

    course_package_id: str
    course_package_checksum: str
    tokenizer_version: str
    documents: tuple[_LexicalDocument, ...]
    postings: tuple[tuple[str, tuple[str, ...]], ...]
    checksum: str

    def __post_init__(self) -> None:
        _require(type(self.documents) is tuple, "snapshot documents must be a tuple")
        _require(type(self.postings) is tuple, "snapshot postings must be a tuple")
        for posting in self.postings:
            _require(
                type(posting) is tuple
                and len(posting) == 2
                and type(posting[1]) is tuple,
                "snapshot postings must contain tuple IDs",
            )


def _canonical_bytes(value: Any) -> bytes:
    try:
        return dumps_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("canonical JSON is invalid") from None


def _require_utf8(value: object, message: str) -> None:
    _require(isinstance(value, str), message)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(message) from None


def _fold_text(value: str) -> str:
    _require_utf8(value, "lexical text is invalid")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _lexical_token_occurrences(value: str) -> tuple[str, ...]:
    """Return tokenizer occurrences before the public unique-token projection."""

    normalized = _fold_text(value)
    atomic_tokens: list[str] = []
    bigrams: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if "\u4e00" <= character <= "\u9fff":
            atomic_tokens.append(character)
            if index + 1 < len(normalized) and "\u4e00" <= normalized[index + 1] <= "\u9fff":
                bigrams.append(character + normalized[index + 1])
            index += 1
            continue
        match = _ASCII_RUN.match(normalized, index)
        if match is not None:
            atomic_tokens.append(match.group(0))
            index = match.end()
            continue
        index += 1
    return tuple(atomic_tokens + bigrams)


def _lexical_tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_lexical_token_occurrences(value)))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _document_dict(document: _LexicalDocument) -> dict[str, Any]:
    return {
        "concept_ids": list(document.concept_ids),
        "chunk_id": document.chunk_id,
        "evidence_id": document.evidence_id,
        "folded_text": document.folded_text,
        "locator": document.locator,
        "source_id": document.source_id,
        "text": document.text,
        "text_sha256": document.text_sha256,
        "tokens": list(document.tokens),
    }


def _documents_core(snapshot: LexicalIndexSnapshot) -> dict[str, Any]:
    return {
        "course_package_checksum": snapshot.course_package_checksum,
        "course_package_id": snapshot.course_package_id,
        "documents": [_document_dict(document) for document in snapshot.documents],
        "format_version": _FORMAT_VERSION,
        "tokenizer_version": snapshot.tokenizer_version,
    }


def _postings_core(snapshot: LexicalIndexSnapshot) -> dict[str, Any]:
    return {
        "course_package_checksum": snapshot.course_package_checksum,
        "course_package_id": snapshot.course_package_id,
        "format_version": _FORMAT_VERSION,
        "postings": [
            {"chunk_ids": list(chunk_ids), "token": token}
            for token, chunk_ids in snapshot.postings
        ],
        "tokenizer_version": snapshot.tokenizer_version,
    }


def semantic_checksum(snapshot: LexicalIndexSnapshot) -> str:
    """Hash only semantic index identity and canonical payload cores."""

    documents_bytes = _canonical_bytes(_documents_core(snapshot))
    postings_bytes = _canonical_bytes(_postings_core(snapshot))
    return _sha256(
        _canonical_bytes(
            {
                "course_package_checksum": snapshot.course_package_checksum,
                "course_package_id": snapshot.course_package_id,
                "documents_sha256": _sha256(documents_bytes),
                "format_version": _FORMAT_VERSION,
                "postings_sha256": _sha256(postings_bytes),
                "tokenizer_version": snapshot.tokenizer_version,
            }
        )
    )


def snapshot_to_payloads(snapshot: LexicalIndexSnapshot) -> dict[str, bytes]:
    """Encode one validated snapshot as the two exact canonical payloads."""

    _validate_snapshot(snapshot)
    documents = {**_documents_core(snapshot), "index_checksum": snapshot.checksum}
    postings = {**_postings_core(snapshot), "index_checksum": snapshot.checksum}
    return {
        _DOCUMENTS_PAYLOAD: _canonical_bytes(documents),
        _POSTINGS_PAYLOAD: _canonical_bytes(postings),
    }


def _decode_payload(payload: bytes, name: str) -> dict[str, Any]:
    _require(isinstance(payload, bytes), f"{name} must be bytes")
    try:
        decoded = payload.decode("utf-8")
        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = item
            return result

        value = json.loads(decoded, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError(f"{name} must be canonical JSON") from None
    _require(type(value) is dict, f"{name} must be an object")
    try:
        canonical = _canonical_bytes(value)
    except ValueError:
        raise ValueError(f"{name} must be canonical JSON") from None
    _require(canonical == payload, f"{name} is not canonical")
    return value


def _parse_document(value: object) -> _LexicalDocument:
    _require(type(value) is dict, "document must be an object")
    expected = {
        "concept_ids",
        "chunk_id",
        "evidence_id",
        "folded_text",
        "locator",
        "source_id",
        "text",
        "text_sha256",
        "tokens",
    }
    _require(set(value) == expected, "document keys are invalid")
    for name in expected - {"concept_ids", "tokens"}:
        _require_utf8(value[name], f"document {name} is invalid")
    concepts, tokens = value["concept_ids"], value["tokens"]
    _require(type(concepts) is list, "document concepts are invalid")
    _require(type(tokens) is list, "document tokens are invalid")
    for concept in concepts:
        _require_utf8(concept, "document concepts are invalid")
    for token in tokens:
        _require_utf8(token, "document tokens are invalid")
    return _LexicalDocument(
        evidence_id=value["evidence_id"],
        chunk_id=value["chunk_id"],
        source_id=value["source_id"],
        text=value["text"],
        locator=value["locator"],
        concept_ids=tuple(concepts),
        text_sha256=value["text_sha256"],
        folded_text=value["folded_text"],
        tokens=tuple(tokens),
    )


def snapshot_from_payloads(payloads: Mapping[str, bytes]) -> LexicalIndexSnapshot:
    """Decode and strictly validate the two canonical M2 lexical payloads."""

    _require(isinstance(payloads, Mapping), "payloads must be a mapping")
    _require(set(payloads) == {_DOCUMENTS_PAYLOAD, _POSTINGS_PAYLOAD}, "payload names are invalid")
    documents_payload = _decode_payload(payloads[_DOCUMENTS_PAYLOAD], _DOCUMENTS_PAYLOAD)
    postings_payload = _decode_payload(payloads[_POSTINGS_PAYLOAD], _POSTINGS_PAYLOAD)
    document_keys = {
        "course_package_checksum", "course_package_id", "documents", "format_version",
        "index_checksum", "tokenizer_version",
    }
    posting_keys = {
        "course_package_checksum", "course_package_id", "format_version", "index_checksum",
        "postings", "tokenizer_version",
    }
    _require(set(documents_payload) == document_keys, "documents payload keys are invalid")
    _require(set(postings_payload) == posting_keys, "postings payload keys are invalid")
    shared_names = {"course_package_checksum", "course_package_id", "format_version", "index_checksum", "tokenizer_version"}
    _require(all(documents_payload[name] == postings_payload[name] for name in shared_names), "payload bindings disagree")
    _require(type(documents_payload["format_version"]) is int and documents_payload["format_version"] == _FORMAT_VERSION, "format version is invalid")
    _require_utf8(documents_payload["tokenizer_version"], "tokenizer version is invalid")
    for name in ("course_package_id", "course_package_checksum", "index_checksum"):
        _require_utf8(documents_payload[name], "payload identity is invalid")
    _require(type(documents_payload["documents"]) is list, "documents are invalid")
    documents = tuple(_parse_document(item) for item in documents_payload["documents"])
    _require(type(postings_payload["postings"]) is list, "postings are invalid")
    postings: list[tuple[str, tuple[str, ...]]] = []
    for row in postings_payload["postings"]:
        _require(type(row) is dict and set(row) == {"token", "chunk_ids"}, "posting is invalid")
        _require_utf8(row["token"], "posting token is invalid")
        _require(type(row["chunk_ids"]) is list, "posting chunk IDs are invalid")
        for chunk_id in row["chunk_ids"]:
            _require_utf8(chunk_id, "posting chunk IDs are invalid")
        postings.append((row["token"], tuple(row["chunk_ids"])))
    snapshot = LexicalIndexSnapshot(
        course_package_id=documents_payload["course_package_id"],
        course_package_checksum=documents_payload["course_package_checksum"],
        tokenizer_version=documents_payload["tokenizer_version"],
        documents=documents,
        postings=tuple(postings),
        checksum=documents_payload["index_checksum"],
    )
    _validate_snapshot(snapshot)
    return snapshot


def _validate_snapshot(snapshot: LexicalIndexSnapshot) -> None:
    """Validate semantic/canonical invariants shared by compile and restore."""

    _require(isinstance(snapshot, LexicalIndexSnapshot), "snapshot is invalid")
    _require(type(snapshot.documents) is tuple and type(snapshot.postings) is tuple, "snapshot collections are invalid")
    _require_utf8(snapshot.course_package_id, "package ID is invalid")
    _require(bool(snapshot.course_package_id), "package ID is invalid")
    _require(_is_sha256(snapshot.course_package_checksum), "package checksum is invalid")
    _require(snapshot.tokenizer_version == _TOKENIZER_VERSION, "tokenizer version is invalid")
    _require(_is_sha256(snapshot.checksum), "index checksum is invalid")
    _require(bool(snapshot.documents), "ready lexical snapshots require documents")
    identifiers: set[str] = set()
    evidence_ids: set[str] = set()
    order: list[tuple[str, str, str]] = []
    expected_postings: dict[str, list[str]] = {}
    for document in snapshot.documents:
        _require(isinstance(document, _LexicalDocument), "document is invalid")
        _require(type(document.concept_ids) is tuple and type(document.tokens) is tuple, "document collections are invalid")
        for value in (
            document.evidence_id,
            document.chunk_id,
            document.source_id,
            document.text,
            document.locator,
            document.text_sha256,
            document.folded_text,
        ):
            _require_utf8(value, "document text is invalid")
        for value in (*document.concept_ids, *document.tokens):
            _require_utf8(value, "document collections are invalid")
        _require(bool(document.chunk_id) and bool(document.evidence_id) and bool(document.source_id) and bool(document.locator), "document identity is invalid")
        _require(document.chunk_id not in identifiers and document.evidence_id not in evidence_ids, "duplicate document identity")
        _require(_is_sha256(document.text_sha256), "document text checksum is invalid")
        text_checksum = _sha256(document.text.encode("utf-8"))
        _require(text_checksum == document.text_sha256, "document text checksum mismatch")
        from course_insight.contracts.evidence import evidence_id_for_chunk

        try:
            expected_evidence_id = evidence_id_for_chunk(document.chunk_id)
        except DomainError:
            raise ValueError("document evidence identity is invalid") from None
        _require(document.evidence_id == expected_evidence_id, "document evidence identity is invalid")
        _require(document.folded_text == _fold_text(document.text), "document folded text is invalid")
        _require(document.tokens == _lexical_tokens(document.text), "document tokens are invalid")
        _require(tuple(sorted(set(document.concept_ids))) == document.concept_ids, "document concepts are not canonical")
        _require(len(set(document.tokens)) == len(document.tokens), "document tokens are duplicated")
        identifiers.add(document.chunk_id)
        evidence_ids.add(document.evidence_id)
        order.append((document.chunk_id, document.source_id, document.locator))
        for token in document.tokens:
            expected_postings.setdefault(token, []).append(document.chunk_id)
    _require(order == sorted(order), "documents are not sorted")
    for posting in snapshot.postings:
        _require(type(posting) is tuple and len(posting) == 2, "posting is invalid")
        token, chunk_ids = posting
        _require_utf8(token, "posting token is invalid")
        _require(type(chunk_ids) is tuple, "posting chunk IDs are invalid")
        for chunk_id in chunk_ids:
            _require_utf8(chunk_id, "posting chunk IDs are invalid")
    expected = tuple((token, tuple(chunk_ids)) for token, chunk_ids in sorted(expected_postings.items()))
    _require(snapshot.postings == expected, "postings are not canonical")
    _require(snapshot.checksum == semantic_checksum(snapshot), "index checksum mismatch")


__all__ = ["LexicalIndexSnapshot", "_LexicalDocument", "semantic_checksum", "snapshot_from_payloads", "snapshot_to_payloads"]
