from __future__ import annotations

import math
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    InMemoryVectorStore,
    MAX_PGVECTOR_DIMENSION,
    PgVectorStore,
    VectorIndexMetadata,
    VectorDocument,
    build_pgvector_exact_search_sql,
    cosine_similarity,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _PgBatchCursor:
    def __init__(self, connection: "_PgBatchConnection") -> None:
        self._connection = connection

    def executemany(
        self,
        statement: str,
        parameters: list[tuple[Any, ...]],
    ) -> None:
        self._connection.executemany_calls += 1
        for values in parameters:
            self._connection.documents.append(values)


class _PgBatchConnection:
    def __init__(self) -> None:
        self.index = {"dimension": 2, "status": "staging"}
        self.documents: list[tuple[Any, ...]] = []
        self.executemany_calls = 0
        self.transaction_entries = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        snapshot = list(self.documents)
        self.transaction_entries += 1
        try:
            yield
        except Exception:
            self.documents = snapshot
            raise

    def execute(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> Any:
        normalized = " ".join(statement.lower().split())
        if "from m2_vector_documents" in normalized:
            index_id, index_version, evidence_ids = parameters
            wanted = set(evidence_ids)
            return _Rows(
                [
                    {"evidence_id": values[2]}
                    for values in self.documents
                    if values[0] == index_id
                    and values[1] == index_version
                    and values[2] in wanted
                ]
            )
        if "select dimension,status" in normalized:
            return _Rows([self.index])
        raise AssertionError(f"unexpected SQL: {statement}")

    @contextmanager
    def cursor(self) -> Iterator[_PgBatchCursor]:
        yield _PgBatchCursor(self)


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.rows)


class _PgBatchPool:
    def __init__(self) -> None:
        self.connection_value = _PgBatchConnection()
        self.checkout_count = 0

    @contextmanager
    def connection(self) -> Iterator[_PgBatchConnection]:
        self.checkout_count += 1
        yield self.connection_value


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


def test_in_memory_add_many_is_bounded_and_validation_is_atomic() -> None:
    store = InMemoryVectorStore()
    store.begin("index-1", "v1", dimension=2)
    valid = VectorDocument("e1", "c1", (1.0, 0.0), "a" * 64)
    invalid = VectorDocument("e2", "c2", (0.0,), "b" * 64)

    with pytest.raises(DomainError, match="vector dimension"):
        store.add_many("index-1", "v1", [valid, invalid])

    store.add_many("index-1", "v1", [valid])
    store.publish("index-1", "v1", expected_count=1, checksum="c" * 64)
    assert store.ready("index-1", "v1") is True


def test_vector_store_begin_uses_shared_dimension_upper_bound() -> None:
    assert "vector(16000)" in build_pgvector_exact_search_sql(
        MAX_PGVECTOR_DIMENSION
    )
    with pytest.raises(ValueError, match="dimension"):
        build_pgvector_exact_search_sql(MAX_PGVECTOR_DIMENSION + 1)

    store = InMemoryVectorStore()
    store.begin("max-index", "v1", dimension=MAX_PGVECTOR_DIMENSION)

    with pytest.raises(DomainError, match="vector dimension"):
        InMemoryVectorStore().begin(
            "too-large",
            "v1",
            dimension=MAX_PGVECTOR_DIMENSION + 1,
        )

    pool = _PgBatchPool()
    with pytest.raises(DomainError, match="vector dimension"):
        PgVectorStore(pool).begin(
            "too-large",
            "v1",
            dimension=MAX_PGVECTOR_DIMENSION + 1,
        )
    assert pool.checkout_count == 0


def test_pgvector_add_many_uses_one_executemany_per_batch_and_rolls_back() -> None:
    pool = _PgBatchPool()
    store = PgVectorStore(pool)
    store.add_many(
        "index-1",
        "v1",
        [
            VectorDocument("e1", "c1", (1.0, 0.0), "a" * 64),
            VectorDocument("e2", "c2", (0.0, 1.0), "b" * 64),
        ],
    )
    assert pool.checkout_count == 1
    assert pool.connection_value.transaction_entries == 1
    assert pool.connection_value.executemany_calls == 1
    assert len(pool.connection_value.documents) == 2

    with pytest.raises(DomainError, match="duplicate vector"):
        store.add_many(
            "index-1",
            "v1",
            [VectorDocument("e1", "c1-again", (1.0, 0.0), "c" * 64)],
        )
    assert pool.connection_value.executemany_calls == 1
    assert len(pool.connection_value.documents) == 2

    with pytest.raises(DomainError, match="vector dimension"):
        store.add_many(
            "index-1",
            "v1",
            [
                VectorDocument("e3", "c3", (1.0, 0.0), "c" * 64),
                VectorDocument("e4", "c4", (0.0,), "d" * 64),
            ],
        )
    assert pool.connection_value.executemany_calls == 1
    assert len(pool.connection_value.documents) == 2


def test_cosine_similarity_is_bounded_and_zero_vector_is_safe() -> None:
    assert cosine_similarity((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0
