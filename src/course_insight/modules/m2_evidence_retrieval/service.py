"""Formal M2 evidence-retrieval service boundary."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
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
from course_insight.contracts.intelligence import RetrievalAudit
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
    compile_snapshot,
    rank_snapshot,
    snapshot_to_payloads,
)
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository


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
    ) -> None:
        self._index_dir = index_dir
        self._tokenizer_or_embedding_adapter = tokenizer_or_embedding_adapter
        self._repository = repository
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
        """Return the unchanged path-free empty pgvector declaration."""

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

    def empty_retrieval_audit(
        self, index_ref: EvidenceIndexRef, requested_at: datetime
    ) -> RetrievalAudit:
        """Record the unchanged unconfigured RAG empty result."""

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
        by_evidence_id = {row.evidence_id: row for row in ranked}
        required_rows: list[Any] = []
        try:
            for evidence_id in sorted(set(query.required_evidence_ids)):
                # Reverse parse first so forged prefixes never fall through to lookup.
                chunk_id_for_evidence_id(evidence_id)
                row = by_evidence_id.get(evidence_id)
                if row is None:
                    raise ValueError
                required_rows.append(row)
        except Exception:
            raise _error("REQUIRED_EVIDENCE_INVALID", "required evidence is unavailable") from None
        required_ids = {row.evidence_id for row in required_rows}
        minimum = Decimal(str(query.min_relevance))
        supplements = [
            row
            for row in ranked
            if row.evidence_id not in required_ids
            and row.score_points > 0
            and Decimal(str(row.relevance)) >= minimum
        ][: query.top_k]
        return EvidenceBundle(
            query_id=query.query_id,
            index_id=stored_ref.index_id,
            course_id=package.course_id,
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            index_checksum=stored_ref.checksum,
            evidence_chunks=[
                *(self._evidence_chunk(row) for row in required_rows),
                *(self._evidence_chunk(row) for row in supplements),
            ],
            retrieved_at=stored_ref.built_at,
        )
