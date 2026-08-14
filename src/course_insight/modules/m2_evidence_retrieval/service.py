"""Formal M2 evidence-retrieval service boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import (
    EvidenceBundle,
    EvidenceChunk,
    EvidenceIndexRef,
    EvidenceQuery,
    chunk_id_for_evidence_id,
    evidence_id_for_chunk,
)
from course_insight.contracts.intelligence import RetrievalAudit, RetrievalPolicy
from course_insight.modules.m2_evidence_retrieval.audit import (
    RetrievalAuditStore,
    create_retrieval_audit,
    persist_retrieval_audit,
)
from course_insight.modules.m2_evidence_retrieval.embedding import (
    EmbeddingProvider,
)
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
    compile_snapshot,
    rank_snapshot,
    snapshot_to_payloads,
)
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository
from course_insight.modules.m2_evidence_retrieval.ranking import (
    rank_hybrid,
    validate_strategy_dependencies,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    VectorDocument,
    VectorIndexMetadata,
    VectorStore,
)


_LOGGER = logging.getLogger(__name__)


def _error(code: str, message: str, *, recoverable: bool = False) -> DomainError:
    """Create one safe public M2 error without caller or filesystem details."""

    return DomainError(
        code=code, module="m2", message=message, details={}, recoverable=recoverable
    )


class M2EvidenceRetrievalService:
    """Build and query explicitly loaded, course-bound lexical indexes."""

    def __init__(
        self,
        index_dir: Path,
        tokenizer_or_embedding_adapter: Any,
        repository: M2Repository,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        audit_store: RetrievalAuditStore | None = None,
        production: bool = False,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._index_dir = index_dir
        self._tokenizer_or_embedding_adapter = tokenizer_or_embedding_adapter
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._audit_store = audit_store
        self._production = production
        self._clock = clock or time.monotonic_ns
        self._indexed_packages: dict[tuple[str, str], CoursePackage] = {}
        self._index_refs: dict[tuple[str, str], EvidenceIndexRef] = {}
        self._snapshots: dict[tuple[str, str], LexicalIndexSnapshot] = {}

    @staticmethod
    def _key(index: EvidenceIndexRef | tuple[str, str]) -> tuple[str, str]:
        return (
            (index.index_id, index.index_version)
            if isinstance(index, EvidenceIndexRef)
            else index
        )

    @staticmethod
    def _validated_package(course_package: CoursePackage) -> CoursePackage:
        """Copy and revalidate the governed M1 value before touching M2 state."""

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
            return package
        except Exception:
            raise _error(
                "INDEX_NOT_READY", "course package is not ready for lexical indexing", recoverable=True
            ) from None

    @staticmethod
    def _validated_ref(index: EvidenceIndexRef) -> EvidenceIndexRef:
        try:
            if not isinstance(index, EvidenceIndexRef):
                raise ValueError
            result = EvidenceIndexRef.model_validate(
                index.model_dump(mode="python", warnings="error")
            )
            if result.status != "ready":
                raise ValueError
            return result
        except Exception:
            raise _error("INDEX_ARTIFACT_INVALID", "evidence index is invalid") from None

    @staticmethod
    def _assert_ref_matches_package(
        ref: EvidenceIndexRef, package: CoursePackage
    ) -> None:
        if (
            ref.backend != "lexical"
            or ref.embedding_model_id is not None
            or ref.storage_ref != f"lexical:{ref.index_id}"
            or ref.course_package_checksum is None
            or not ref.matches(package)
        ):
            raise _error("INDEX_ARTIFACT_INVALID", "evidence index is invalid")

    @staticmethod
    def _assert_snapshot_matches_package(
        snapshot: LexicalIndexSnapshot, package: CoursePackage
    ) -> None:
        """Check stored governed document values without reconstructing postings."""

        try:
            if (
                snapshot.course_package_id != package.course_package_id
                or snapshot.course_package_checksum != package.checksum
                or len(snapshot.documents) != len(package.content_chunks)
            ):
                raise ValueError
            chunks = {chunk.chunk_id: chunk for chunk in package.content_chunks}
            if len(chunks) != len(package.content_chunks):
                raise ValueError
            for document in snapshot.documents:
                chunk = chunks.get(document.chunk_id)
                if chunk is None or (
                    document.evidence_id != evidence_id_for_chunk(chunk.chunk_id)
                    or document.source_id != chunk.source_id
                    or document.text != chunk.text
                    or document.locator != chunk.locator
                    or document.concept_ids != tuple(sorted(set(chunk.concept_hints)))
                    or document.text_sha256 != chunk.sha256
                ):
                    raise ValueError
        except Exception:
            raise _error("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid") from None

    def _publish_loaded(
        self,
        *,
        package: CoursePackage,
        ref: EvidenceIndexRef,
        snapshot: LexicalIndexSnapshot,
    ) -> EvidenceIndexRef:
        """Atomically publish defensive state only after all checks and persistence."""

        key = self._key(ref)
        new_packages = dict(self._indexed_packages)
        new_refs = dict(self._index_refs)
        new_snapshots = dict(self._snapshots)
        new_packages[key] = package.model_copy(deep=True)
        new_refs[key] = ref.model_copy(deep=True)
        new_snapshots[key] = snapshot
        self._indexed_packages = new_packages
        self._index_refs = new_refs
        self._snapshots = new_snapshots
        return ref.model_copy(deep=True)

    def build_index(self, course_package: CoursePackage) -> EvidenceIndexRef:
        """Compile, persist, then atomically make one M1 package retrievable."""

        package = self._validated_package(course_package)
        try:
            snapshot = compile_snapshot(package)
        except Exception:
            raise _error(
                "INDEX_NOT_READY", "course package is not ready for lexical indexing", recoverable=True
            ) from None
        index_id = f"{package.course_package_id}_lexical_index"
        ref = EvidenceIndexRef(
            index_id=index_id,
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            index_version=package.package_version,
            storage_ref=f"lexical:{index_id}",
            backend="lexical",
            embedding_model_id=None,
            source_count=len(package.source_documents),
            chunk_count=len(package.content_chunks),
            built_at=package.imported_at,
            checksum=snapshot.checksum,
            status="ready",
        )
        # Repository DomainErrors are intentionally public stable storage outcomes.
        try:
            self._repository.save_index_artifact(ref, snapshot)
        except DomainError:
            raise
        except Exception:
            raise _error("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid") from None
        return self._publish_loaded(package=package, ref=ref, snapshot=snapshot)

    def restore_index(
        self, *, course_package: CoursePackage, evidence_index_ref: EvidenceIndexRef
    ) -> EvidenceIndexRef:
        """Load exactly one artifact and validate it; never compile it."""

        package = self._validated_package(course_package)
        requested_ref = self._validated_ref(evidence_index_ref)
        self._assert_ref_matches_package(requested_ref, package)
        try:
            loaded = self._repository.load_index_artifact(
                requested_ref.index_id, requested_ref.index_version
            )
        except DomainError:
            raise
        except Exception:
            raise _error("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid") from None
        if loaded is None:
            raise _error("INDEX_NOT_READY", "evidence index is not available", recoverable=True)
        try:
            stored_ref, snapshot = loaded
            stored_ref = self._validated_ref(stored_ref)
            if stored_ref.model_dump(mode="json") != requested_ref.model_dump(mode="json"):
                raise ValueError
            self._assert_ref_matches_package(stored_ref, package)
            if (
                snapshot.course_package_id != stored_ref.course_package_id
                or snapshot.course_package_checksum != stored_ref.course_package_checksum
                or snapshot.checksum != stored_ref.checksum
            ):
                raise ValueError
            # Canonical serialization performs the full frozen snapshot validation,
            # including postings and semantic checksum, without compiling/rebuilding.
            snapshot_to_payloads(snapshot)
            self._assert_snapshot_matches_package(snapshot, package)
        except DomainError:
            raise
        except Exception:
            raise _error("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid") from None
        return self._publish_loaded(package=package, ref=stored_ref, snapshot=snapshot)

    def initialize_vector_store(
        self, course_package_id: str, requested_at: datetime
    ) -> EvidenceIndexRef:
        """Validate the configured vector path and return a usable declaration."""

        if self._embedding_provider is None or self._vector_store is None:
            if self._production:
                raise _error(
                    "VECTOR_STORE_UNAVAILABLE",
                    "vector store is unavailable",
                    recoverable=True,
                )
            return self._empty_vector_ref(course_package_id, requested_at)
        model = self._embedding_provider.model_ref
        index_id = f"{course_package_id}_vector_index"
        return EvidenceIndexRef(
            index_id=index_id,
            course_package_id=course_package_id,
            course_package_checksum=None,
            index_version=model.model_version,
            storage_ref=f"pgvector:{index_id}",
            backend="pgvector",
            embedding_model_id=(
                f"{model.provider}:{model.model_name}:{model.model_version}:"
                f"{model.dimension}"
            ),
            source_count=0,
            chunk_count=0,
            built_at=requested_at,
            checksum=hashlib.sha256(index_id.encode("utf-8")).hexdigest(),
            status="building",
        )

    @staticmethod
    def _empty_vector_ref(course_package_id: str, requested_at: datetime) -> EvidenceIndexRef:
        """Keep the historical explicit empty result for offline discovery only."""

        index_id = f"pgvector_empty_{course_package_id}"
        checksum = hashlib.sha256(
            f"pgvector:empty:{course_package_id}".encode("utf-8")
        ).hexdigest()
        return EvidenceIndexRef(
            index_id=index_id,
            course_package_id=course_package_id,
            index_version="unconfigured",
            storage_ref=f"pgvector:unconfigured:{course_package_id}",
            backend="pgvector",
            embedding_model_id=None,
            source_count=0,
            chunk_count=0,
            built_at=requested_at,
            checksum=checksum,
            status="empty",
        )

    def build_vector_index(
        self,
        course_package: CoursePackage,
        *,
        index_version: str | None = None,
    ) -> EvidenceIndexRef:
        """Embed every governed chunk, stage it, then publish one ready index."""

        if self._embedding_provider is None or self._vector_store is None:
            raise _error("VECTOR_STORE_UNAVAILABLE", "vector store is unavailable", recoverable=True)
        package = self._validated_package(course_package)
        provider = self._embedding_provider
        model = provider.model_ref
        version = index_version or model.model_version
        index_id = f"{package.course_package_id}_vector_index"
        texts = [chunk.text for chunk in package.content_chunks]
        try:
            # Keep a lexical snapshot beside the vector ref so hybrid retrieval
            # remains available after a vector index is built or restored.
            lexical_snapshot = compile_snapshot(package)
        except Exception:
            raise _error(
                "INDEX_NOT_READY",
                "course package is not ready for retrieval indexing",
                recoverable=True,
            ) from None
        try:
            vectors = provider.embed_documents(texts)
            if len(vectors) != len(texts):
                raise ValueError
            self._vector_store.begin(index_id, version, dimension=model.dimension)
            rows = []
            for chunk, vector in zip(package.content_chunks, vectors):
                rows.append(
                    VectorDocument(
                        evidence_id=evidence_id_for_chunk(chunk.chunk_id),
                        chunk_id=chunk.chunk_id,
                        vector=tuple(vector),
                        text_checksum=chunk.sha256,
                    )
                )
            for row in rows:
                self._vector_store.add(index_id, version, row)
            checksum = hashlib.sha256(
                json.dumps(
                    [
                        {
                            "evidence_id": row.evidence_id,
                            "chunk_id": row.chunk_id,
                            "text_checksum": row.text_checksum,
                            "vector": list(row.vector),
                        }
                        for row in rows
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            built_at = datetime.now(timezone.utc)
            self._vector_store.publish(
                index_id,
                version,
                expected_count=len(rows),
                checksum=checksum,
                metadata=VectorIndexMetadata(
                    index_checksum=checksum,
                    course_package_id=package.course_package_id,
                    course_package_checksum=package.checksum,
                    embedding_model_id=(
                        f"{model.provider}:{model.model_name}:{model.model_version}:"
                        f"{model.dimension}"
                    ),
                    dimension=model.dimension,
                    source_count=len(package.source_documents),
                    chunk_count=len(package.content_chunks),
                    built_at=built_at,
                ),
            )
        except DomainError:
            raise
        except Exception:
            raise _error(
                "VECTOR_INDEX_BUILD_FAILED",
                "vector index could not be built",
                recoverable=True,
            ) from None
        ref = EvidenceIndexRef(
            index_id=index_id,
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            index_version=version,
            storage_ref=f"pgvector:{index_id}",
            backend="pgvector",
            embedding_model_id=(
                f"{model.provider}:{model.model_name}:{model.model_version}:"
                f"{model.dimension}"
            ),
            source_count=len(package.source_documents),
            chunk_count=len(package.content_chunks),
            built_at=built_at,
            checksum=checksum,
            status="ready",
        )
        return self._publish_loaded(
            package=package,
            ref=ref,
            snapshot=lexical_snapshot,
        )

    def restore_vector_index(
        self,
        *,
        course_package: CoursePackage,
        evidence_index_ref: EvidenceIndexRef,
    ) -> EvidenceIndexRef:
        """Restore a ready pgvector declaration without rebuilding embeddings."""

        if self._embedding_provider is None or self._vector_store is None:
            raise _error("VECTOR_STORE_UNAVAILABLE", "vector store is unavailable", recoverable=True)
        package = self._validated_package(course_package)
        ref = self._validated_ref(evidence_index_ref)
        model = self._embedding_provider.model_ref
        expected_model_id = (
            f"{model.provider}:{model.model_name}:{model.model_version}:{model.dimension}"
        )
        if (
            ref.backend != "pgvector"
            or not ref.matches(package)
            or ref.embedding_model_id != expected_model_id
            or ref.storage_ref != f"pgvector:{ref.index_id}"
            or not self._vector_store.ready(ref.index_id, ref.index_version)
        ):
            raise _error("INDEX_NOT_READY", "vector index is not available", recoverable=True)
        metadata_reader = getattr(self._vector_store, "get_metadata", None)
        if not callable(metadata_reader):
            raise _error(
                "INDEX_NOT_READY",
                "vector index metadata is unavailable",
                recoverable=True,
            )
        try:
            metadata = metadata_reader(ref.index_id, ref.index_version)
        except DomainError:
            raise _error(
                "INDEX_NOT_READY",
                "vector index metadata is unavailable",
                recoverable=True,
            ) from None
        if (
            not isinstance(metadata, VectorIndexMetadata)
            or metadata.index_checksum != ref.checksum
            or metadata.course_package_id != package.course_package_id
            or metadata.course_package_checksum != package.checksum
            or metadata.embedding_model_id != expected_model_id
            or metadata.dimension != model.dimension
            or metadata.source_count != len(package.source_documents)
            or metadata.chunk_count != len(package.content_chunks)
            or metadata.built_at != ref.built_at
        ):
            raise _error(
                "INDEX_NOT_READY",
                "vector index metadata does not match the governed package",
                recoverable=True,
            )
        try:
            snapshot = compile_snapshot(package)
        except Exception:
            raise _error("INDEX_NOT_READY", "course package is not ready for retrieval indexing", recoverable=True) from None
        return self._publish_loaded(package=package, ref=ref, snapshot=snapshot)

    def restore_vector_index_from_store(
        self,
        *,
        course_package: CoursePackage,
        index_id: str,
        index_version: str,
    ) -> EvidenceIndexRef:
        """Discover a ready vector reference from durable store metadata."""

        if self._embedding_provider is None or self._vector_store is None:
            raise _error(
                "VECTOR_STORE_UNAVAILABLE",
                "vector store is unavailable",
                recoverable=True,
            )
        package = self._validated_package(course_package)
        metadata_reader = getattr(self._vector_store, "get_metadata", None)
        if not callable(metadata_reader):
            raise _error(
                "INDEX_NOT_READY",
                "vector index metadata is unavailable",
                recoverable=True,
            )
        try:
            metadata = metadata_reader(index_id, index_version)
        except DomainError:
            raise _error(
                "INDEX_NOT_READY",
                "vector index metadata is unavailable",
                recoverable=True,
            ) from None
        model = self._embedding_provider.model_ref
        expected_model_id = (
            f"{model.provider}:{model.model_name}:{model.model_version}:{model.dimension}"
        )
        if (
            not isinstance(metadata, VectorIndexMetadata)
            or metadata.course_package_id != package.course_package_id
            or metadata.course_package_checksum != package.checksum
            or metadata.embedding_model_id != expected_model_id
            or metadata.dimension != model.dimension
            or metadata.source_count != len(package.source_documents)
            or metadata.chunk_count != len(package.content_chunks)
        ):
            raise _error(
                "INDEX_NOT_READY",
                "vector index metadata does not match the governed package",
                recoverable=True,
            )
        ref = EvidenceIndexRef(
            index_id=index_id,
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            index_version=index_version,
            storage_ref=f"pgvector:{index_id}",
            backend="pgvector",
            embedding_model_id=metadata.embedding_model_id,
            source_count=metadata.source_count,
            chunk_count=metadata.chunk_count,
            built_at=metadata.built_at,
            checksum=metadata.index_checksum,
            status="ready",
        )
        return self.restore_vector_index(
            course_package=package,
            evidence_index_ref=ref,
        )

    @staticmethod
    def _select_ranked_results(
        *,
        query: EvidenceQuery,
        package: CoursePackage,
        ranked: Sequence[tuple[str, float]],
    ) -> tuple[tuple[str, float], ...]:
        """Apply the shared required/min-relevance/top-k retrieval contract."""

        chunks = {
            evidence_id_for_chunk(chunk.chunk_id): chunk
            for chunk in package.content_chunks
        }
        scores = dict(ranked)
        required: list[tuple[str, float]] = []
        try:
            for evidence_id in sorted(set(query.required_evidence_ids)):
                chunk_id_for_evidence_id(evidence_id)
                if evidence_id not in chunks:
                    raise ValueError
                required.append(
                    (
                        evidence_id,
                        max(0.0, min(1.0, float(scores.get(evidence_id, 0.0)))),
                    )
                )
        except (DomainError, TypeError, ValueError):
            raise _error(
                "REQUIRED_EVIDENCE_INVALID",
                "required evidence is unavailable",
            ) from None

        required_ids = {evidence_id for evidence_id, _ in required}
        minimum = Decimal(str(query.min_relevance))
        supplements: list[tuple[str, float]] = []
        seen: set[str] = set(required_ids)
        for evidence_id, raw_score in ranked:
            if evidence_id in seen or evidence_id not in chunks:
                continue
            relevance = max(0.0, min(1.0, float(raw_score)))
            if raw_score > 0 and Decimal(str(relevance)) >= minimum:
                supplements.append((evidence_id, relevance))
                seen.add(evidence_id)
                if len(supplements) >= query.top_k:
                    break
        return tuple([*required, *supplements])

    def _latency_ms(self, started_ns: int) -> int:
        return max(0, int((self._clock() - started_ns) // 1_000_000))

    @staticmethod
    def _audit_bundle(
        *,
        audit_store: RetrievalAuditStore | None,
        query: EvidenceQuery,
        index: EvidenceIndexRef,
        policy: RetrievalPolicy,
        bundle: EvidenceBundle,
        request_id: str | None,
        created_at: datetime,
        latency_ms: int,
    ) -> None:
        if audit_store is None:
            return
        envelope = create_retrieval_audit(
            query=query,
            index=index,
            policy=policy,
            status="succeeded" if bundle.evidence_chunks else "empty",
            evidence_ids=[chunk.evidence_id for chunk in bundle.evidence_chunks],
            scores=[chunk.relevance for chunk in bundle.evidence_chunks],
            latency_ms=latency_ms,
            request_id=request_id or query.query_id,
            created_at=created_at,
        )
        persist_retrieval_audit(audit_store, envelope)

    @staticmethod
    def _audit_failure(
        *,
        audit_store: RetrievalAuditStore | None,
        query: EvidenceQuery,
        index: EvidenceIndexRef | None,
        policy: RetrievalPolicy,
        request_id: str | None,
        created_at: datetime,
        latency_ms: int,
    ) -> None:
        """Record a failed formal retrieval when a ready index identity exists."""

        if audit_store is None or index is None or index.status != "ready":
            return
        envelope = create_retrieval_audit(
            query=query,
            index=index,
            policy=policy,
            status="failed",
            evidence_ids=[],
            scores=[],
            latency_ms=latency_ms,
            request_id=request_id or query.query_id,
            created_at=created_at,
        )
        try:
            persist_retrieval_audit(audit_store, envelope)
        except Exception as error:
            # Preserve the original business failure; expose only the safe
            # exception type so secrets or provider payloads cannot be logged.
            _LOGGER.error(
                "retrieval failure audit persistence failed (%s)",
                type(error).__name__,
            )

    def retrieve_with_policy(
        self,
        evidence_query: EvidenceQuery,
        evidence_index_ref: EvidenceIndexRef,
        policy: RetrievalPolicy,
        *,
        request_id: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> EvidenceBundle:
        """Execute lexical/vector/hybrid retrieval through configured ports."""

        policy.validate_business_rules()
        started_ns = self._clock()
        if self._production and self._audit_store is None:
            raise _error(
                "RETRIEVAL_AUDIT_UNAVAILABLE",
                "retrieval audit is unavailable",
                recoverable=True,
            )
        try:
            evidence_query = EvidenceQuery.model_validate(
                evidence_query.model_dump(mode="python", warnings="error")
            )
            evidence_index_ref = self._validated_ref(evidence_index_ref)
        except DomainError:
            raise
        except Exception:
            raise _error("EVIDENCE_QUERY_INVALID", "evidence query or index is invalid") from None
        key = self._key(evidence_index_ref)
        package = self._indexed_packages.get(key)
        ref = self._index_refs.get(key)
        snapshot = self._snapshots.get(key)
        lexical_available = package is not None and snapshot is not None
        vector_available = False
        if (
            self._embedding_provider is not None
            and self._vector_store is not None
            and ref is not None
            and ref.backend == "pgvector"
        ):
            try:
                vector_available = self._vector_store.ready(
                    ref.index_id, ref.index_version
                )
            except Exception:
                # Dependency health is evaluated before the governed failure
                # boundary. Treat a health-check error as unavailable so the
                # dependency failure is still audited without leaking adapter
                # details.
                vector_available = False
        audit_index = ref or evidence_index_ref
        try:
            validate_strategy_dependencies(
                policy.strategy,
                lexical_available=lexical_available,
                vector_available=vector_available,
            )
        except DomainError:
            self._audit_failure(
                audit_store=self._audit_store,
                query=evidence_query,
                index=audit_index,
                policy=policy,
                request_id=request_id,
                created_at=retrieved_at or datetime.now(timezone.utc),
                latency_ms=self._latency_ms(started_ns),
            )
            raise
        try:
            if policy.strategy == "lexical":
                bundle = self.retrieve(evidence_query, evidence_index_ref)
            else:
                if (
                    package is None
                    or ref is None
                    or self._embedding_provider is None
                    or self._vector_store is None
                ):
                    raise _error("INDEX_NOT_READY", "evidence index is not loaded", recoverable=True)
                if (
                    evidence_query.course_package_id != package.course_package_id
                    or (
                        evidence_query.course_package_checksum is not None
                        and evidence_query.course_package_checksum != package.checksum
                    )
                ):
                    raise _error("INDEX_NOT_READY", "evidence index is not loaded", recoverable=True)
                try:
                    query_vector = self._embedding_provider.embed_query(
                        evidence_query.query_text
                    )
                except Exception:
                    raise _error(
                        "EMBEDDING_PROVIDER_UNAVAILABLE",
                        "embedding provider is unavailable",
                        recoverable=True,
                    ) from None
                try:
                    vector_matches = self._vector_store.search(
                        ref.index_id,
                        ref.index_version,
                        query_vector,
                        top_k=len(package.content_chunks),
                    )
                except DomainError:
                    raise
                except Exception:
                    raise _error(
                        "VECTOR_SEARCH_FAILED",
                        "vector search failed",
                        recoverable=True,
                    ) from None
                vector_scores = {match.evidence_id: match.score for match in vector_matches}
                lexical_scores = {
                    row.evidence_id: row.relevance
                    for row in rank_snapshot(
                        snapshot,
                        query_text=evidence_query.query_text,
                        concept_ids=evidence_query.concept_ids,
                    )
                } if snapshot is not None else {}
                if policy.strategy == "vector":
                    ranked = tuple(
                        sorted(vector_scores.items(), key=lambda item: (-item[1], item[0]))
                    )
                else:
                    ranked_candidates = rank_hybrid(
                        lexical_scores,
                        vector_scores,
                        lexical_weight=policy.lexical_weight,
                        vector_weight=policy.vector_weight,
                    )
                    ranked = tuple(
                        (candidate.evidence_id, candidate.final_score)
                        for candidate in ranked_candidates
                    )
                selected = self._select_ranked_results(
                    query=evidence_query,
                    package=package,
                    ranked=ranked,
                )
                now = retrieved_at or datetime.now(timezone.utc)
                chunks = {
                    evidence_id_for_chunk(chunk.chunk_id): chunk
                    for chunk in package.content_chunks
                }
                bundle = EvidenceBundle(
                    query_id=evidence_query.query_id,
                    index_id=ref.index_id,
                    course_id=package.course_id,
                    course_package_id=package.course_package_id,
                    course_package_checksum=package.checksum,
                    index_checksum=ref.checksum,
                    evidence_chunks=[
                        EvidenceChunk(
                            evidence_id=evidence_id,
                            source_id=chunks[evidence_id].source_id,
                            chunk_id=chunks[evidence_id].chunk_id,
                            text=chunks[evidence_id].text,
                            locator=chunks[evidence_id].locator,
                            concept_ids=list(chunks[evidence_id].concept_hints),
                            relevance=relevance,
                            checksum=chunks[evidence_id].sha256,
                        )
                        for evidence_id, relevance in selected
                    ],
                    retrieved_at=now,
                )
        except DomainError:
            self._audit_failure(
                audit_store=self._audit_store,
                query=evidence_query,
                index=audit_index,
                policy=policy,
                request_id=request_id,
                created_at=retrieved_at or datetime.now(timezone.utc),
                latency_ms=self._latency_ms(started_ns),
            )
            raise
        except Exception:
            self._audit_failure(
                audit_store=self._audit_store,
                query=evidence_query,
                index=audit_index,
                policy=policy,
                request_id=request_id,
                created_at=retrieved_at or datetime.now(timezone.utc),
                latency_ms=self._latency_ms(started_ns),
            )
            raise _error(
                "RETRIEVAL_FAILED",
                "retrieval failed",
                recoverable=True,
            ) from None
        self._audit_bundle(
            audit_store=self._audit_store,
            query=evidence_query,
            index=audit_index,
            policy=policy,
            bundle=bundle,
            request_id=request_id,
            created_at=bundle.retrieved_at,
            latency_ms=self._latency_ms(started_ns),
        )
        return bundle

    def empty_retrieval_audit(
        self, index_ref: EvidenceIndexRef, requested_at: datetime
    ) -> RetrievalAudit:
        """Record the unchanged unconfigured RAG empty result."""

        if self._production:
            raise _error(
                "RETRIEVAL_AUDIT_UNAVAILABLE",
                "retrieval audit is unavailable",
                recoverable=True,
            )

        return RetrievalAudit(
            audit_id=f"retrieval_empty_{index_ref.index_id}",
            query_id=f"query_empty_{index_ref.course_package_id}",
            index_id=index_ref.index_id,
            policy_id="retrieval_policy_unconfigured",
            retrieved_evidence_ids=[],
            status="empty",
            created_at=requested_at,
        )

    @staticmethod
    def _evidence_chunk(row: Any) -> EvidenceChunk:
        return EvidenceChunk(
            evidence_id=row.evidence_id,
            source_id=row.source_id,
            chunk_id=row.chunk_id,
            text=row.text,
            locator=row.locator,
            concept_ids=list(row.concept_ids),
            relevance=row.relevance,
            checksum=row.text_sha256,
        )

    def retrieve(
        self, evidence_query: EvidenceQuery, evidence_index_ref: EvidenceIndexRef
    ) -> EvidenceBundle:
        """Query only an already-loaded lexical snapshot; never rebuild it."""

        try:
            if not isinstance(evidence_query, EvidenceQuery):
                raise ValueError
            query = EvidenceQuery.model_validate(
                evidence_query.model_dump(mode="python", warnings="error")
            )
        except DomainError:
            raise
        except Exception:
            raise _error("EVIDENCE_QUERY_INVALID", "evidence query is invalid") from None
        try:
            requested_ref = self._validated_ref(evidence_index_ref)
        except DomainError:
            raise _error("INDEX_NOT_READY", "evidence index is not loaded", recoverable=True) from None
        key = self._key(requested_ref)
        package = self._indexed_packages.get(key)
        stored_ref = self._index_refs.get(key)
        snapshot = self._snapshots.get(key)
        if (
            package is None
            or stored_ref is None
            or snapshot is None
            or requested_ref.model_dump(mode="json") != stored_ref.model_dump(mode="json")
            or query.course_package_id != package.course_package_id
            or (
                query.course_package_checksum is not None
                and query.course_package_checksum != package.checksum
            )
        ):
            raise _error("INDEX_NOT_READY", "evidence index is not loaded", recoverable=True)
        try:
            ranked = rank_snapshot(
                snapshot, query_text=query.query_text, concept_ids=query.concept_ids
            )
        except Exception:
            raise _error("EVIDENCE_QUERY_INVALID", "evidence query is invalid") from None
        selected = self._select_ranked_results(
            query=query,
            package=package,
            ranked=tuple((row.evidence_id, row.relevance) for row in ranked),
        )
        rows = {
            row.evidence_id: row
            for row in ranked
        }
        return EvidenceBundle(
            query_id=query.query_id,
            index_id=stored_ref.index_id,
            course_id=package.course_id,
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            index_checksum=stored_ref.checksum,
            evidence_chunks=[
                self._evidence_chunk(
                    replace(rows[evidence_id], relevance=relevance)
                )
                for evidence_id, relevance in selected
            ],
            retrieved_at=stored_ref.built_at,
        )
