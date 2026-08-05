from __future__ import annotations

import hashlib
import json
from collections import UserDict
from types import MappingProxyType
from datetime import datetime, timezone
from pathlib import Path

import pytest

from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import (
    EvidenceChunk,
    EvidenceBundle,
    EvidenceIndexRef,
    EvidenceQuery,
    chunk_id_for_evidence_id,
    evidence_id_for_chunk,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m2_evidence_retrieval.stubs import (
    M2EvidenceRetrievalServiceStub,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _blank_evidence_chunk(
    *,
    chunk_id: str = "chunk_1",
    evidence_id: str | None = None,
) -> EvidenceChunk:
    return EvidenceChunk(
        evidence_id=(
            evidence_id_for_chunk(chunk_id)
            if evidence_id is None
            else evidence_id
        ),
        source_id="source_1",
        chunk_id=chunk_id,
        text="A governed rule explains the target concept.",
        locator="section:1",
        concept_ids=["concept_1"],
        relevance=1.0,
        checksum="a" * 64,
    )


def _course_package(*, chunk_id: str = "chunk_1") -> CoursePackage:
    source = SourceDocument(
        source_id="source_1",
        file_name="course.md",
        media_type="text/markdown",
        sha256="b" * 64,
        page_count=None,
        title="Course",
        version="1.0.0",
    )

    chunk = ContentChunk(
        chunk_id=chunk_id,
        source_id=source.source_id,
        text="A governed rule explains the target concept.",
        locator="section:1",
        concept_hints=["concept_1"],
        sha256=hashlib.sha256(
            "A governed rule explains the target concept.".encode("utf-8")
        ).hexdigest(),
    )
    package = CoursePackage(
        course_package_id="package_1",
        course_id="course_1",
        package_version="1.0.0",
        source_documents=[source],
        content_chunks=[chunk],
        source_authorizations=[
            SourceAuthorization(
                source_id=source.source_id,
                authorized_by="teacher_1",
                license_note="Approved for the course.",
                authorized_at=NOW,
            )
        ],
        imported_at=NOW,
        status="ready",
        checksum="c" * 64,
    )
    return package.model_copy(update={"checksum": package.recalculate_checksum()})


def _legacy_index_json() -> dict[str, object]:
    """Return an M2 index payload emitted before additive package binding."""

    return {
        "index_id": "index_1",
        "course_package_id": "package_1",
        "index_version": "1.0.0",
        "storage_ref": "lexical:index_1",
        "backend": "lexical",
        "embedding_model_id": None,
        "source_count": 1,
        "chunk_count": 1,
        "built_at": "2026-08-03T00:00:00Z",
        "checksum": "d" * 64,
        "status": "ready",
        "schema_version": "1.0.0",
    }


def _legacy_query_json() -> dict[str, object]:
    """Return an M6/M8 query payload emitted before additive bindings."""

    return {
        "query_id": "query_1",
        "course_package_id": "package_1",
        "query_text": "target concept",
        "concept_ids": ["concept_1"],
        "item_id": None,
        "use_case": "qa",
        "top_k": 1,
        "min_relevance": 0.0,
        "schema_version": "1.0.0",
    }


def _legacy_bundle_json() -> dict[str, object]:
    """Return an M2 bundle payload emitted before additive index bindings."""

    return {
        "query_id": "query_1",
        "index_id": "index_1",
        "course_id": "course_1",
        "evidence_chunks": [],
        "retrieved_at": "2026-08-03T00:00:00Z",
        "schema_version": "1.0.0",
    }


def test_evidence_chunk_accepts_canonical_id_for_its_chunk() -> None:
    """Protection test: canonical IDs were already accepted before this rule."""

    chunk = _blank_evidence_chunk(chunk_id="chunk_7")

    assert chunk.evidence_id == "evidence_chunk_7"


def test_evidence_id_for_blank_chunk_rejects_stably() -> None:
    with pytest.raises(DomainError) as captured:
        evidence_id_for_chunk("   ")

    assert captured.value.code == "EVIDENCE_ID_MISMATCH"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


def test_evidence_id_for_nonblank_chunk_preserves_original_value() -> None:
    assert evidence_id_for_chunk(" chunk_1 ") == "evidence_ chunk_1 "


@pytest.mark.parametrize("value", [None, 1, "", "\x00", "chunk\x00_1"])
def test_evidence_identity_helpers_reject_invalid_forward_input_stably(
    value: object,
) -> None:
    with pytest.raises(DomainError) as captured:
        evidence_id_for_chunk(value)  # type: ignore[arg-type]

    assert captured.value.code == "EVIDENCE_ID_MISMATCH"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


@pytest.mark.parametrize("value", [None, 1, "", "\x00", "evidence_chunk\x00_1"])
def test_evidence_identity_helpers_reject_invalid_reverse_input_stably(
    value: object,
) -> None:
    with pytest.raises(DomainError) as captured:
        chunk_id_for_evidence_id(value)  # type: ignore[arg-type]

    assert captured.value.code == "EVIDENCE_ID_MISMATCH"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


def test_chunk_id_for_evidence_id_requires_exact_canonical_form() -> None:
    assert chunk_id_for_evidence_id("evidence_chunk_alpha") == "chunk_alpha"
    assert chunk_id_for_evidence_id("evidence_ chunk_alpha ") == " chunk_alpha "
    assert chunk_id_for_evidence_id("evidence_\uff43\uff48\uff55\uff4e\uff4b_alpha") == "\uff43\uff48\uff55\uff4e\uff4b_alpha"

    for value in (
        "chunk_alpha",
        "evidence_",
        "evidence_ \t",
        "evidence_a\x00b",
    ):
        with pytest.raises(DomainError) as captured:
            chunk_id_for_evidence_id(value)
        assert captured.value.code == "EVIDENCE_ID_MISMATCH"
        assert captured.value.module == "m2"
        assert captured.value.details == {}


def test_legacy_evidence_models_keep_defaults_and_cache_binds_new_query_fields() -> None:
    index = EvidenceIndexRef.model_validate(_legacy_index_json())
    query = EvidenceQuery.model_validate(_legacy_query_json())
    bundle = EvidenceBundle.model_validate(_legacy_bundle_json())

    assert index.course_package_checksum is None
    assert query.course_package_checksum is None
    assert query.required_evidence_ids == []
    assert bundle.course_package_id is None
    assert bundle.course_package_checksum is None
    assert bundle.index_checksum is None
    assert query.cache_key() != query.model_copy(
        update={"required_evidence_ids": ["evidence_chunk_1"]}
    ).cache_key()
    assert query.cache_key() != query.model_copy(
        update={"course_package_checksum": "f" * 64}
    ).cache_key()


def test_default_evidence_bindings_preserve_legacy_serialization_and_checksum() -> None:
    index = EvidenceIndexRef.model_validate(_legacy_index_json())
    query = EvidenceQuery.model_validate(_legacy_query_json())
    bundle = EvidenceBundle.model_validate(_legacy_bundle_json())

    assert index.to_dict() == _legacy_index_json()
    assert query.to_dict() == _legacy_query_json()
    assert bundle.to_dict() == _legacy_bundle_json()
    assert query.content_checksum() == hashlib.sha256(
        json.dumps(
            _legacy_query_json(),
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    bound = query.model_copy(
        update={
            "course_package_checksum": "f" * 64,
            "required_evidence_ids": ["evidence_chunk_1"],
        }
    )
    assert bound.to_dict()["course_package_checksum"] == "f" * 64
    assert bound.to_dict()["required_evidence_ids"] == ["evidence_chunk_1"]
    assert bound.content_checksum() != query.content_checksum()


def test_evidence_query_cache_treats_concepts_and_required_ids_as_sets() -> None:
    first = EvidenceQuery(
        **(
            _legacy_query_json()
            | {
                "concept_ids": ["concept_b", "concept_a", "concept_b"],
                "required_evidence_ids": [
                    "evidence_chunk_b",
                    "evidence_chunk_a",
                    "evidence_chunk_b",
                ],
            }
        ),
    )
    second = EvidenceQuery(
        **(
            _legacy_query_json()
            | {
                "concept_ids": ["concept_a", "concept_b"],
                "required_evidence_ids": [
                    "evidence_chunk_a",
                    "evidence_chunk_b",
                ],
            }
        ),
    )

    assert first.cache_key() == second.cache_key()


def test_evidence_query_required_ids_default_is_not_shared() -> None:
    first = EvidenceQuery.model_validate(_legacy_query_json())
    second = EvidenceQuery.model_validate(_legacy_query_json())

    first.required_evidence_ids.append("evidence_chunk_1")

    assert first.required_evidence_ids == ["evidence_chunk_1"]
    assert second.required_evidence_ids == []


def test_evidence_index_matches_package_checksum_only_when_bound() -> None:
    package = _course_package()
    legacy_ref = EvidenceIndexRef.model_validate(_legacy_index_json())
    bound_ref = EvidenceIndexRef(
        **(
            _legacy_index_json()
            | {"course_package_checksum": "different-package-checksum"}
        ),
    )
    empty_checksum_ref = EvidenceIndexRef(
        **(_legacy_index_json() | {"course_package_checksum": ""}),
    )

    assert legacy_ref.matches(package)
    assert not bound_ref.matches(package)
    assert not empty_checksum_ref.matches(package)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("course_package_id", "package_\ud800"),
        ("course_package_checksum", "checksum_\ud800"),
        ("query_text", "retrieve \ud800 evidence"),
        ("concept_ids", ["concept_\ud800"]),
        ("required_evidence_ids", ["evidence_\ud800"]),
        ("item_id", "item_\ud800"),
    ],
)
def test_evidence_query_rejects_lone_surrogates_before_cache_encoding(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(DomainError) as captured:
        EvidenceQuery(**(_legacy_query_json() | {field_name: value}))

    assert captured.value.code == "EVIDENCE_QUERY_INVALID"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


def test_evidence_query_legal_unicode_cache_key_is_stable() -> None:
    query = EvidenceQuery(
        query_id="query_1",
        course_package_id="课程_1",
        query_text="  课程  知识  ",
        concept_ids=["概念_b", "概念_a"],
        item_id="条目_1",
        use_case="qa",
        top_k=1,
        min_relevance=0.0,
    )

    assert query.cache_key() == (
        "e30a4b5fd78e7dbfeb3f0ddb001493ac9be268a9e2f753bd857da9ec24e08461"
    )


@pytest.mark.parametrize("mapping_type", [MappingProxyType, UserDict])
def test_evidence_query_rejects_surrogate_semantics_from_any_mapping(
    mapping_type: type[object],
) -> None:
    payload = _legacy_query_json() | {"course_package_checksum": "bad_\ud800"}

    with pytest.raises(DomainError) as captured:
        EvidenceQuery.model_validate(mapping_type(payload))  # type: ignore[call-arg]

    assert captured.value.code == "EVIDENCE_QUERY_INVALID"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


@pytest.mark.parametrize(
    "update",
    [
        {"course_package_checksum": "bad_\ud800"},
        {"item_id": "item_\ud800"},
    ],
)
def test_evidence_query_cache_key_rejects_model_copy_surrogates(
    update: dict[str, str],
) -> None:
    query = EvidenceQuery.model_validate(_legacy_query_json()).model_copy(
        update=update,
    )

    with pytest.raises(DomainError) as captured:
        query.cache_key()

    assert captured.value.code == "EVIDENCE_QUERY_INVALID"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


@pytest.mark.parametrize("field_name", ["concept_ids", "required_evidence_ids"])
def test_evidence_query_cache_key_rejects_in_place_list_surrogates(
    field_name: str,
) -> None:
    query = EvidenceQuery.model_validate(_legacy_query_json())
    getattr(query, field_name).append("bad_\ud800")

    with pytest.raises(DomainError) as captured:
        query.cache_key()

    assert captured.value.code == "EVIDENCE_QUERY_INVALID"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


def test_evidence_query_rejects_surrogate_on_assignment() -> None:
    query = EvidenceQuery.model_validate(_legacy_query_json())

    with pytest.raises(DomainError) as captured:
        query.concept_ids = ["bad_\ud800"]

    assert captured.value.code == "EVIDENCE_QUERY_INVALID"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


def test_evidence_chunk_rejects_id_for_a_different_chunk() -> None:
    with pytest.raises(DomainError) as captured:
        _blank_evidence_chunk(
            chunk_id="chunk_7",
            evidence_id="evidence_chunk_8",
        )

    assert captured.value.code == "EVIDENCE_ID_MISMATCH"
    assert captured.value.module == "m2"
    assert captured.value.details == {}


def test_m2_retrieval_keeps_evidence_id_canonical_for_returned_chunk(
    tmp_path: Path,
) -> None:
    """Protection test: M2 already creates IDs in the canonical format."""

    package = _course_package()
    service = M2EvidenceRetrievalServiceStub(tmp_path)
    index_ref = service.build_index(package)

    result = service.retrieve(
        EvidenceQuery(
            query_id="query_1",
            course_package_id=package.course_package_id,
            query_text="target concept",
            concept_ids=["concept_1"],
            item_id=None,
            use_case="qa",
            top_k=1,
            min_relevance=0.0,
        ),
        index_ref,
    )

    assert [(chunk.evidence_id, chunk.chunk_id) for chunk in result.evidence_chunks] == [
        ("evidence_chunk_1", "chunk_1")
    ]


def test_m2_retrieval_preserves_nonblank_chunk_id_whitespace(
    tmp_path: Path,
) -> None:
    """Protection test: retrieval preserves the source chunk ID exactly."""

    package = _course_package(chunk_id=" chunk_1 ")
    service = M2EvidenceRetrievalServiceStub(tmp_path)
    index_ref = service.build_index(package)

    result = service.retrieve(
        EvidenceQuery(
            query_id="query_1",
            course_package_id=package.course_package_id,
            query_text="target concept",
            concept_ids=["concept_1"],
            item_id=None,
            use_case="qa",
            top_k=1,
            min_relevance=0.0,
        ),
        index_ref,
    )

    assert [(chunk.evidence_id, chunk.chunk_id) for chunk in result.evidence_chunks] == [
        ("evidence_ chunk_1 ", " chunk_1 ")
    ]
