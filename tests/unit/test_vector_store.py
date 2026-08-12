from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    InMemoryVectorStore,
    VectorIndexMetadata,
    VectorDocument,
    cosine_similarity,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_in_memory_vector_store_publishes_only_complete_dimension_bound_batch() -> None:
    store = InMemoryVectorStore()
    store.begin("index-1", "v1", dimension=2)
    store.add(
        "index-1", "v1", VectorDocument("e1", "c1", (1.0, 0.0), "a" * 64)
    )
    store.add(
        "index-1", "v1", VectorDocument("e2", "c2", (0.0, 1.0), "b" * 64)
    )
    with pytest.raises(DomainError, match="not complete"):
        store.publish("index-1", "v1", expected_count=3, checksum="c" * 64)
    assert store.ready("index-1", "v1") is False
    store.publish("index-1", "v1", expected_count=2, checksum="c" * 64)
    assert store.ready("index-1", "v1") is True
    assert [row.evidence_id for row in store.search("index-1", "v1", (0.9, 0.1), top_k=2)] == ["e1", "e2"]


def test_in_memory_vector_store_persists_and_returns_published_metadata() -> None:
    store = InMemoryVectorStore()
    store.begin("index-1", "v1", dimension=2)
    store.add(
        "index-1", "v1", VectorDocument("e1", "c1", (1.0, 0.0), "a" * 64)
    )
    metadata = VectorIndexMetadata(
        index_checksum="c" * 64,
        course_package_id="package-1",
        course_package_checksum="b" * 64,
        embedding_model_id="test:model:v1:2",
        dimension=2,
        source_count=1,
        chunk_count=1,
        built_at=NOW,
    )
    store.publish(
        "index-1",
        "v1",
        expected_count=1,
        checksum="c" * 64,
        metadata=metadata,
    )

    assert store.get_metadata("index-1", "v1") == metadata


def test_vector_restore_rejects_persisted_metadata_bound_to_another_package() -> None:
    from pathlib import Path

    from course_insight.contracts.errors import DomainError
    from course_insight.modules.m2_evidence_retrieval.embedding import (
        DeterministicEmbeddingProvider,
        EmbeddingModelIdentity,
    )
    from course_insight.modules.m2_evidence_retrieval.service import (
        M2EvidenceRetrievalService,
    )
    from tests.unit.test_m2_evidence_retrieval_mvp import (
        _MemoryArtifactRepository,
        _package,
    )

    class CorruptingStore(InMemoryVectorStore):
        def get_metadata(self, index_id: str, index_version: str):
            current = super().get_metadata(index_id, index_version)
            return replace(current, course_package_checksum="d" * 64)

    package = _package()
    provider = DeterministicEmbeddingProvider.for_tests(
        model_ref=EmbeddingModelIdentity(
            provider="test",
            model_name="restore-test",
            model_version="v1",
            dimension=4,
        )
    )
    store = CorruptingStore()
    service = M2EvidenceRetrievalService(
        Path("runtime/vector-restore-integrity"),
        "lexical",
        _MemoryArtifactRepository(),
        embedding_provider=provider,
        vector_store=store,
        production=True,
    )
    index = service.build_vector_index(package)

    with pytest.raises(DomainError) as raised:
        service.restore_vector_index(
            course_package=package,
            evidence_index_ref=index,
        )

    assert raised.value.code == "INDEX_NOT_READY"


def test_vector_store_rejects_nonfinite_wrong_dimension_and_duplicate_rows() -> None:
    store = InMemoryVectorStore()
    store.begin("index-1", "v1", dimension=2)
    with pytest.raises(DomainError, match="vector is invalid"):
        store.add("index-1", "v1", VectorDocument("e1", "c1", (math.nan, 0.0), "a" * 64))
    with pytest.raises(DomainError, match="vector dimension"):
        store.add("index-1", "v1", VectorDocument("e1", "c1", (1.0,), "a" * 64))
    store.add("index-1", "v1", VectorDocument("e1", "c1", (1.0, 0.0), "a" * 64))
    with pytest.raises(DomainError, match="duplicate vector"):
        store.add("index-1", "v1", VectorDocument("e1", "c1", (0.0, 1.0), "a" * 64))


def test_cosine_similarity_is_bounded_and_zero_vector_is_safe() -> None:
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0
