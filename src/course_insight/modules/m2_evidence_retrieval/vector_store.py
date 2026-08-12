"""Vector-store ports with a fail-closed in-memory adapter and pgvector adapter."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from course_insight.contracts.errors import DomainError


@dataclass(frozen=True, slots=True)
class VectorDocument:
    evidence_id: str
    chunk_id: str
    vector: tuple[float, ...]
    text_checksum: str


@dataclass(frozen=True, slots=True)
class VectorMatch:
    evidence_id: str
    chunk_id: str
    score: float
    text_checksum: str


@dataclass(frozen=True, slots=True)
class VectorIndexMetadata:
    """Durable bindings required to prove a vector index after restart."""

    index_checksum: str
    course_package_id: str
    course_package_checksum: str
    embedding_model_id: str
    dimension: int
    source_count: int
    chunk_count: int
    built_at: datetime


class VectorStore(Protocol):
    """Storage port for versioned, staged vector indexes."""

    def begin(self, index_id: str, index_version: str, *, dimension: int) -> None:
        """Create or replace an unpublished staging batch."""

    def add(self, index_id: str, index_version: str, document: VectorDocument) -> None:
        """Add one validated vector to a staging batch."""

    def publish(
        self,
        index_id: str,
        index_version: str,
        *,
        expected_count: int,
        checksum: str,
        metadata: VectorIndexMetadata | None = None,
    ) -> None:
        """Atomically mark a complete staging batch as ready."""

    def ready(self, index_id: str, index_version: str) -> bool:
        """Return whether the exact version is published and ready."""

    def search(self, index_id: str, index_version: str, query_vector: Sequence[float], *, top_k: int) -> tuple[VectorMatch, ...]:
        """Search only a ready exact version."""

    def get_metadata(
        self, index_id: str, index_version: str
    ) -> VectorIndexMetadata | None:
        """Return durable package/model bindings for a ready version."""


class InMemoryVectorStore:
    """Deterministic protocol adapter for tests and local development."""

    def __init__(self) -> None:
        self._staging: dict[tuple[str, str], tuple[int, dict[str, VectorDocument]]] = {}
        self._ready: dict[
            tuple[str, str],
            tuple[str, int, tuple[VectorDocument, ...], VectorIndexMetadata | None],
        ] = {}

    def begin(self, index_id: str, index_version: str, *, dimension: int) -> None:
        _validate_identity(index_id, index_version)
        if type(dimension) is not int or dimension < 1:
            raise _invalid("vector dimension is invalid")
        self._staging[(index_id, index_version)] = (dimension, {})

    def add(self, index_id: str, index_version: str, document: VectorDocument) -> None:
        key = (index_id, index_version)
        batch = self._staging.get(key)
        if batch is None:
            raise _invalid("vector staging batch is missing")
        dimension, rows = batch
        _validate_document(document, dimension)
        if document.evidence_id in rows:
            raise _invalid("duplicate vector document")
        rows[document.evidence_id] = document

    def publish(
        self,
        index_id: str,
        index_version: str,
        *,
        expected_count: int,
        checksum: str,
        metadata: VectorIndexMetadata | None = None,
    ) -> None:
        key = (index_id, index_version)
        batch = self._staging.get(key)
        if batch is None or type(expected_count) is not int or expected_count < 1:
            raise _invalid("vector staging batch is missing")
        if len(batch[1]) != expected_count:
            raise _invalid("vector batch is not complete")
        _validate_checksum(checksum)
        if metadata is not None:
            _validate_metadata(metadata, checksum, batch[0], expected_count)
        rows = tuple(sorted(batch[1].values(), key=lambda row: row.evidence_id))
        self._ready[key] = (checksum, batch[0], rows, metadata)
        del self._staging[key]

    def ready(self, index_id: str, index_version: str) -> bool:
        return (index_id, index_version) in self._ready

    def search(self, index_id: str, index_version: str, query_vector: Sequence[float], *, top_k: int) -> tuple[VectorMatch, ...]:
        key = (index_id, index_version)
        stored = self._ready.get(key)
        if stored is None:
            raise _invalid("vector index is not ready")
        if type(top_k) is not int or top_k < 1:
            raise _invalid("vector top-k is invalid")
        _validate_vector(tuple(query_vector), stored[1])
        matches = [
            VectorMatch(
                evidence_id=row.evidence_id,
                chunk_id=row.chunk_id,
                score=cosine_similarity(tuple(query_vector), row.vector),
                text_checksum=row.text_checksum,
            )
            for row in stored[2]
        ]
        matches.sort(key=lambda match: (-match.score, match.evidence_id))
        return tuple(matches[:top_k])

    def get_metadata(
        self, index_id: str, index_version: str
    ) -> VectorIndexMetadata | None:
        stored = self._ready.get((index_id, index_version))
        if stored is None:
            raise _invalid("vector index is not ready")
        return stored[3]


class PgVectorStore:
    """PostgreSQL+pgvector adapter; migrations own the table and extension."""

    def __init__(self, pool: object) -> None:
        self._pool = pool

    def assert_available(self) -> None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') AS enabled"
                ).fetchone()
        except Exception:
            raise DomainError(
                code="VECTOR_STORE_UNAVAILABLE",
                module="m2",
                message="pgvector store is unavailable",
                recoverable=True,
            ) from None
        if not row or row.get("enabled") is not True:
            raise DomainError(
                code="VECTOR_STORE_UNAVAILABLE",
                module="m2",
                message="pgvector extension is unavailable",
                recoverable=True,
            )

    def begin(self, index_id: str, index_version: str, *, dimension: int) -> None:
        _validate_identity(index_id, index_version)
        if type(dimension) is not int or dimension < 1:
            raise _invalid("vector dimension is invalid")
        self.assert_available()
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "DELETE FROM m2_vector_documents WHERE index_id = %s AND index_version = %s",
                        (index_id, index_version),
                    )
                    connection.execute(
                        "DELETE FROM m2_vector_indexes WHERE index_id = %s AND index_version = %s",
                        (index_id, index_version),
                    )
                    connection.execute(
                        "INSERT INTO m2_vector_indexes(index_id,index_version,dimension,status) VALUES (%s,%s,%s,'staging')",
                        (index_id, index_version, dimension),
                    )
        except DomainError:
            raise
        except Exception:
            raise DomainError(code="VECTOR_STORE_UNAVAILABLE", module="m2", message="pgvector store is unavailable", recoverable=True) from None

    def add(self, index_id: str, index_version: str, document: VectorDocument) -> None:
        dimension = self._dimension(index_id, index_version)
        _validate_document(document, dimension)
        vector = "[" + ",".join(format(value, ".17g") for value in document.vector) + "]"
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "INSERT INTO m2_vector_documents(index_id,index_version,evidence_id,chunk_id,text_checksum,dimension,embedding) VALUES (%s,%s,%s,%s,%s,%s,%s::vector)",
                    (
                        index_id,
                        index_version,
                        document.evidence_id,
                        document.chunk_id,
                        document.text_checksum,
                        dimension,
                        vector,
                    ),
                )
        except Exception:
            raise DomainError(code="VECTOR_STORE_WRITE_FAILED", module="m2", message="vector document could not be stored") from None

    def publish(
        self,
        index_id: str,
        index_version: str,
        *,
        expected_count: int,
        checksum: str,
        metadata: VectorIndexMetadata | None = None,
    ) -> None:
        if type(expected_count) is not int or expected_count < 1:
            raise _invalid("vector batch is not complete")
        _validate_checksum(checksum)
        if metadata is not None:
            _validate_metadata(metadata, checksum, 0, expected_count)
        if metadata is not None:
            raise _invalid("metadata persistence is unavailable")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        "SELECT dimension, status FROM m2_vector_indexes WHERE index_id=%s AND index_version=%s FOR UPDATE",
                        (index_id, index_version),
                    ).fetchone()
                    count = connection.execute(
                        "SELECT COUNT(*) AS count FROM m2_vector_documents WHERE index_id=%s AND index_version=%s",
                        (index_id, index_version),
                    ).fetchone()
                    if not row or row.get("status") != "staging" or count.get("count") != expected_count:
                        raise _invalid("vector batch is not complete")
                    connection.execute(
                        "UPDATE m2_vector_indexes SET status='ready', checksum=%s, chunk_count=%s WHERE index_id=%s AND index_version=%s",
                        (checksum, expected_count, index_id, index_version),
                    )
        except DomainError:
            raise
        except Exception:
            raise DomainError(code="VECTOR_STORE_WRITE_FAILED", module="m2", message="vector index could not be published") from None

    def ready(self, index_id: str, index_version: str) -> bool:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    "SELECT status FROM m2_vector_indexes WHERE index_id=%s AND index_version=%s",
                    (index_id, index_version),
                ).fetchone()
        except Exception:
            raise DomainError(code="VECTOR_STORE_UNAVAILABLE", module="m2", message="pgvector store is unavailable", recoverable=True) from None
        return bool(row and row.get("status") == "ready")

    def search(self, index_id: str, index_version: str, query_vector: Sequence[float], *, top_k: int) -> tuple[VectorMatch, ...]:
        dimension = self._dimension(index_id, index_version)
        _validate_vector(tuple(query_vector), dimension)
        if type(top_k) is not int or top_k < 1:
            raise _invalid("vector top-k is invalid")
        vector = "[" + ",".join(format(float(value), ".17g") for value in query_vector) + "]"
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    "SELECT evidence_id,chunk_id,text_checksum,1-(embedding <=> %s::vector) AS score FROM m2_vector_documents WHERE index_id=%s AND index_version=%s ORDER BY embedding <=> %s::vector,evidence_id LIMIT %s",
                    (vector, index_id, index_version, vector, top_k),
                ).fetchall()
        except Exception:
            raise DomainError(code="VECTOR_STORE_QUERY_FAILED", module="m2", message="vector search failed", recoverable=True) from None
        return tuple(VectorMatch(row["evidence_id"], row["chunk_id"], float(row["score"]), row["text_checksum"]) for row in rows)

    def get_metadata(
        self, index_id: str, index_version: str
    ) -> VectorIndexMetadata | None:
        raise _invalid("metadata persistence is unavailable")

    def _dimension(self, index_id: str, index_version: str) -> int:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    "SELECT dimension,status FROM m2_vector_indexes WHERE index_id=%s AND index_version=%s",
                    (index_id, index_version),
                ).fetchone()
        except Exception:
            raise DomainError(code="VECTOR_STORE_UNAVAILABLE", module="m2", message="pgvector store is unavailable", recoverable=True) from None
        if not row or row.get("status") not in {"staging", "ready"}:
            raise _invalid("vector index is not ready")
        return int(row["dimension"])


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise _invalid("vector dimension is invalid")
    _validate_vector(tuple(left), len(left))
    _validate_vector(tuple(right), len(right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)))


def _validate_document(document: VectorDocument, dimension: int) -> None:
    if not isinstance(document, VectorDocument):
        raise _invalid("vector document is invalid")
    if len(document.vector) != dimension:
        raise _invalid("vector dimension is invalid")
    _validate_vector(document.vector, dimension)
    if any(not isinstance(value, str) or not value.strip() for value in (document.evidence_id, document.chunk_id)):
        raise _invalid("vector document is invalid")
    _validate_checksum(document.text_checksum)


def _validate_vector(vector: Sequence[float], dimension: int) -> None:
    if len(vector) != dimension or any(type(value) not in {int, float} or not math.isfinite(float(value)) for value in vector):
        raise _invalid("vector is invalid")


def _validate_identity(index_id: str, index_version: str) -> None:
    if any(not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value for value in (index_id, index_version)):
        raise _invalid("vector index identity is invalid")


def _validate_checksum(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise _invalid("vector checksum is invalid")


def _validate_metadata(
    metadata: VectorIndexMetadata,
    checksum: str,
    dimension: int,
    expected_count: int,
) -> None:
    if not isinstance(metadata, VectorIndexMetadata):
        raise _invalid("vector metadata is invalid")
    _validate_checksum(metadata.index_checksum)
    _validate_checksum(metadata.course_package_checksum)
    _validate_checksum(checksum)
    if metadata.index_checksum != checksum:
        raise _invalid("vector metadata checksum conflicts")
    if (
        not metadata.course_package_id.strip()
        or not metadata.embedding_model_id.strip()
        or type(metadata.dimension) is not int
        or metadata.dimension < 1
        or (dimension > 0 and metadata.dimension != dimension)
        or type(metadata.source_count) is not int
        or metadata.source_count < 1
        or type(metadata.chunk_count) is not int
        or metadata.chunk_count != expected_count
        or metadata.built_at.tzinfo is None
        or metadata.built_at.utcoffset() is None
    ):
        raise _invalid("vector metadata is invalid")


def _invalid(message: str) -> DomainError:
    return DomainError(code="VECTOR_INDEX_INVALID", module="m2", message=message)


__all__ = [
    "InMemoryVectorStore",
    "PgVectorStore",
    "VectorDocument",
    "VectorIndexMetadata",
    "VectorMatch",
    "VectorStore",
    "cosine_similarity",
]
