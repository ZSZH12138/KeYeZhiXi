"""Public MVP behavior for persisted, explicit M2 lexical retrieval."""

from __future__ import annotations

import hashlib
from dataclasses import replace
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
from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.intelligence import RetrievalPolicy
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
    compile_snapshot,
    snapshot_from_payloads,
    snapshot_to_payloads,
)
from course_insight.modules.m2_evidence_retrieval.service import M2EvidenceRetrievalService
from course_insight.modules.m2_evidence_retrieval.audit import (
    InMemoryRetrievalAuditStore,
)
from course_insight.modules.m2_evidence_retrieval.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingModelIdentity,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    InMemoryVectorStore,
)
from course_insight.modules.m2_evidence_retrieval.stubs import (
    M2EvidenceRetrievalServiceStub,
    _MemoryM2Repository,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _package(
    *, version: str = "1.0.0", package_id: str = "package_1", changed: bool = False
) -> CoursePackage:
    source = SourceDocument(
        source_id="source_1", file_name="course.md", media_type="text/markdown",
        sha256="a" * 64, page_count=None, title="Course", version=version,
    )
    texts = ["alpha rule", "beta rule", "gamma rule"]
    if changed:
        texts[0] = "changed governed rule"
    chunks = [
        ContentChunk(
            chunk_id=f"chunk_{name}", source_id=source.source_id, text=text,
            locator=f"section:{position}", concept_hints=[f"concept_{name}"],
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        for position, (name, text) in enumerate(zip(("a", "b", "c"), texts), start=1)
    ]
    package = CoursePackage(
        course_package_id=package_id, course_id="course_1", package_version=version,
        source_documents=[source], content_chunks=chunks,
        source_authorizations=[SourceAuthorization(
            source_id=source.source_id, authorized_by="teacher", license_note="approved", authorized_at=NOW,
        )], imported_at=NOW, status="ready", checksum="0" * 64,
    )
    return package.model_copy(update={"checksum": package.recalculate_checksum()})


def _query(package: CoursePackage, **changes: object) -> EvidenceQuery:
    values: dict[str, object] = {
        "query_id": "query_1", "course_package_id": package.course_package_id,
        "query_text": "beta rule", "concept_ids": [], "item_id": None,
        "use_case": "qa", "top_k": 2, "min_relevance": 0.0,
    }
    values.update(changes)
    return EvidenceQuery(**values)


class _MemoryArtifactRepository:
    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str], tuple[EvidenceIndexRef, LexicalIndexSnapshot]] = {}
        self.fail_save = False
        self.corrupt_load = False
        self.load_override: tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None = None

    @staticmethod
    def _clone(snapshot: LexicalIndexSnapshot) -> LexicalIndexSnapshot:
        return snapshot_from_payloads(snapshot_to_payloads(snapshot))

    def save_index(self, index: EvidenceIndexRef) -> None:
        del index

    def get_index(self, index_id: str, index_version: str) -> EvidenceIndexRef | None:
        artifact = self.load_index_artifact(index_id, index_version)
        return None if artifact is None else artifact[0]

    def save_index_artifact(self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot) -> None:
        if self.fail_save:
            raise DomainError(code="INDEX_ARTIFACT_INVALID", module="m2", message="save failed", details={})
        key = (index.index_id, index.index_version)
        candidate = (index.model_copy(deep=True), self._clone(snapshot))
        existing = self.artifacts.get(key)
        if existing is not None and existing != candidate:
            raise DomainError(code="INDEX_VERSION_CONFLICT", module="m2", message="conflict", details={})
        self.artifacts[key] = candidate

    def load_index_artifact(self, index_id: str, index_version: str):
        if self.corrupt_load:
            raise DomainError(code="INDEX_ARTIFACT_INVALID", module="m2", message="corrupt", details={})
        if self.load_override is not None:
            return self.load_override
        candidate = self.artifacts.get((index_id, index_version))
        if candidate is None:
            return None
        return candidate[0].model_copy(deep=True), self._clone(candidate[1])


def _service(repository: _MemoryArtifactRepository) -> M2EvidenceRetrievalService:
    return M2EvidenceRetrievalService(Path("runtime/test-index"), "lexical", repository)


class _FixedEmbeddingProvider:
    model_ref = EmbeddingModelIdentity(
        provider="test",
        model_name="fixed-retrieval",
        model_version="v1",
        dimension=2,
    )
    _vectors = {
        "alpha rule": (1.0, 0.0),
        "beta rule": (0.8, 0.6),
        "gamma rule": (0.1, 0.9949874371),
    }

    def embed_documents(self, texts: object) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vectors[str(text)] for text in texts)  # type: ignore[union-attr]

    def embed_query(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0)


def _vector_service(
    repository: _MemoryArtifactRepository,
    audits: InMemoryRetrievalAuditStore,
    *,
    clock: object | None = None,
) -> M2EvidenceRetrievalService:
    kwargs = {} if clock is None else {"clock": clock}
    return M2EvidenceRetrievalService(
        Path("runtime/test-vector-index"),
        "lexical",
        repository,
        embedding_provider=_FixedEmbeddingProvider(),
        vector_store=InMemoryVectorStore(),
        audit_store=audits,
        production=True,
        **kwargs,
    )


def _policy(strategy: str) -> RetrievalPolicy:
    return RetrievalPolicy(
        policy_id=f"test-{strategy}",
        strategy=strategy,
        top_k=1,
        lexical_weight=1.0 if strategy == "lexical" else 0.5,
        vector_weight=1.0 if strategy == "vector" else 0.5,
        rerank=False,
    )


class _FailingAuditStore:
    def get(self, audit_id: str) -> object:
        del audit_id
        raise RuntimeError("audit backend credentials must not escape")

    def save(self, envelope: object) -> None:
        del envelope
        raise RuntimeError("audit backend credentials must not escape")


def test_required_evidence_is_complete_and_top_k_limits_only_supplements() -> None:
    repository = _MemoryArtifactRepository()
    package = _package()
    service = _service(repository)
    index = service.build_index(package)

    bundle = service.retrieve(_query(
        package, required_evidence_ids=["evidence_chunk_c", "evidence_chunk_a", "evidence_chunk_c"],
        top_k=1, min_relevance=1.0,
    ), index)

    assert bundle.citation_ids() == ["evidence_chunk_a", "evidence_chunk_c", "evidence_chunk_b"]
    assert bundle.course_package_id == package.course_package_id
    assert bundle.course_package_checksum == package.checksum
    assert bundle.index_checksum == index.checksum


@pytest.mark.parametrize("strategy", ["vector", "hybrid"])
def test_vector_and_hybrid_apply_required_and_min_relevance_contract(
    strategy: str,
) -> None:
    repository = _MemoryArtifactRepository()
    audits = InMemoryRetrievalAuditStore()
    package = _package()
    service = _vector_service(repository, audits)
    index = service.build_vector_index(package)

    bundle = service.retrieve_with_policy(
        _query(
            package,
            query_text="unmatched",
            required_evidence_ids=["evidence_chunk_c", "evidence_chunk_a", "evidence_chunk_c"],
            top_k=1,
            min_relevance=1.0,
        ),
        index,
        _policy(strategy),
        request_id=f"request-{strategy}",
    )

    assert bundle.citation_ids() == ["evidence_chunk_a", "evidence_chunk_c"]


@pytest.mark.parametrize("strategy", ["lexical", "vector", "hybrid"])
def test_binding_failure_is_audited_for_all_strategies(
    strategy: str,
) -> None:
    repository = _MemoryArtifactRepository()
    audits = InMemoryRetrievalAuditStore()
    package = _package()
    service = _vector_service(repository, audits)
    index = service.build_vector_index(package)

    with pytest.raises(DomainError) as captured:
        service.retrieve_with_policy(
            _query(package, course_package_checksum="f" * 64),
            index,
            _policy(strategy),
            request_id=f"binding-{strategy}",
        )

    assert captured.value.code == "INDEX_NOT_READY"
    assert len(audits._audits) == 1  # noqa: SLF001
    audit = next(iter(audits._audits.values()))  # noqa: SLF001
    assert audit.audit.status == "failed"
    assert audit.audit.retrieved_evidence_ids == []


def test_success_audit_records_injected_monotonic_latency() -> None:
    repository = _MemoryArtifactRepository()
    audits = InMemoryRetrievalAuditStore()
    ticks = iter((1_000_000_000, 1_006_000_000))
    service = _vector_service(repository, audits, clock=lambda: next(ticks))
    package = _package()
    index = service.build_index(package)

    service.retrieve_with_policy(
        _query(package),
        index,
        RetrievalPolicy(
            policy_id="latency-lexical",
            strategy="lexical",
            top_k=1,
            lexical_weight=1.0,
            vector_weight=0.0,
            rerank=False,
        ),
        request_id="latency-request",
    )

    audit = next(iter(audits._audits.values()))  # noqa: SLF001
    assert audit.metadata.latency_ms == 6


def test_audit_persistence_failure_preserves_original_business_error() -> None:
    repository = _MemoryArtifactRepository()
    package = _package()
    service = M2EvidenceRetrievalService(
        Path("runtime/audit-failure"),
        "lexical",
        repository,
        audit_store=_FailingAuditStore(),  # type: ignore[arg-type]
        production=True,
    )
    index = service.build_index(package)

    with pytest.raises(DomainError) as captured:
        service.retrieve_with_policy(
            _query(package, course_package_checksum="f" * 64),
            index,
            _policy("lexical"),
        )

    assert captured.value.code == "INDEX_NOT_READY"
    assert "credentials" not in str(captured.value)


def test_fresh_service_needs_explicit_restore_and_never_rebuilds() -> None:
    repository = _MemoryArtifactRepository()
    package = _package()
    index = _service(repository).build_index(package)
    fresh = _service(repository)

    with pytest.raises(DomainError) as captured:
        fresh.retrieve(_query(package), index)
    assert captured.value.code == "INDEX_NOT_READY"
    assert captured.value.details == {}
    assert fresh.restore_index(course_package=package, evidence_index_ref=index) == index
    assert fresh.retrieve(_query(package), index).index_checksum == index.checksum


@pytest.mark.parametrize("update", [{"status": "failed"}, {"checksum": "f" * 64}])
def test_build_rejects_nonready_or_rechecksum_invalid_m1(update: dict[str, str]) -> None:
    package = _package().model_copy(update=update)
    with pytest.raises(DomainError) as captured:
        _service(_MemoryArtifactRepository()).build_index(package)
    assert captured.value.code == "INDEX_NOT_READY"
    assert captured.value.details == {}


def test_failed_save_does_not_replace_live_index_state() -> None:
    repository = _MemoryArtifactRepository()
    service = _service(repository)
    first = service.build_index(_package())
    repository.fail_save = True
    with pytest.raises(DomainError) as captured:
        service.build_index(_package(version="2.0.0"))
    assert captured.value.code == "INDEX_ARTIFACT_INVALID"
    assert service.retrieve(_query(_package()), first).index_checksum == first.checksum


def test_restore_missing_corrupt_and_governed_document_mismatch_fail_closed() -> None:
    repository = _MemoryArtifactRepository()
    package = _package()
    index = _service(repository).build_index(package)
    fresh = _service(repository)
    repository.artifacts.clear()
    with pytest.raises(DomainError) as missing:
        fresh.restore_index(course_package=package, evidence_index_ref=index)
    assert missing.value.code == "INDEX_NOT_READY"
    _service(repository).build_index(package)
    repository.corrupt_load = True
    with pytest.raises(DomainError) as corrupt:
        fresh.restore_index(course_package=package, evidence_index_ref=index)
    assert corrupt.value.code == "INDEX_ARTIFACT_INVALID"
    repository.corrupt_load = False
    with pytest.raises(DomainError) as mismatch:
        fresh.restore_index(course_package=_package(changed=True), evidence_index_ref=index)
    assert mismatch.value.code == "INDEX_ARTIFACT_INVALID"
    assert mismatch.value.details == {}


def test_restore_rejects_semantically_invalid_postings_before_live_publication() -> None:
    repository = _MemoryArtifactRepository()
    package = _package()
    index = _service(repository).build_index(package)
    stored_ref, stored_snapshot = repository.artifacts[(index.index_id, index.index_version)]
    repository.load_override = (stored_ref, replace(stored_snapshot, postings=()))
    fresh = _service(repository)

    with pytest.raises(DomainError) as captured:
        fresh.restore_index(course_package=package, evidence_index_ref=index)

    assert captured.value.code == "INDEX_ARTIFACT_INVALID"
    assert captured.value.details == {}
    with pytest.raises(DomainError) as not_published:
        fresh.retrieve(_query(package), index)
    assert not_published.value.code == "INDEX_NOT_READY"


def test_restore_never_calls_compile_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _MemoryArtifactRepository()
    package = _package()
    index = _service(repository).build_index(package)

    def fail_compile(_: CoursePackage) -> LexicalIndexSnapshot:
        raise AssertionError("restore must not compile")

    monkeypatch.setattr(
        "course_insight.modules.m2_evidence_retrieval.service.compile_snapshot",
        fail_compile,
    )
    restored = _service(repository).restore_index(
        course_package=package, evidence_index_ref=index
    )

    assert restored == index


def test_binding_mismatch_and_unknown_or_cross_loaded_required_ids_are_safe() -> None:
    repository = _MemoryArtifactRepository()
    service = _service(repository)
    package = _package()
    index = service.build_index(package)
    other = _package(package_id="package_2")
    other_index = service.build_index(other)
    with pytest.raises(DomainError) as mismatch:
        service.retrieve(_query(package, course_package_checksum="x" * 64), index)
    assert mismatch.value.code == "INDEX_NOT_READY"
    # Create and load an ID belonging only to a different course package.
    cross = _package(package_id="package_3")
    cross_chunk = cross.content_chunks[0].model_copy(update={"chunk_id": "only_other"})
    cross = cross.model_copy(update={"content_chunks": [cross_chunk, *cross.content_chunks[1:]]})
    cross = cross.model_copy(update={"checksum": cross.recalculate_checksum()})
    service.build_index(cross)
    for evidence_id in ("evidence_missing", "evidence_only_other"):
        with pytest.raises(DomainError) as captured:
            service.retrieve(_query(package, required_evidence_ids=[evidence_id]), index)
        assert captured.value.code == "REQUIRED_EVIDENCE_INVALID"
        assert captured.value.details == {}
    assert other_index.index_id != index.index_id


def test_required_zero_score_zero_match_order_tie_and_two_versions() -> None:
    repository = _MemoryArtifactRepository()
    service = _service(repository)
    package_v1 = _package(version="1.0.0")
    package_v2 = _package(version="2.0.0")
    first = service.build_index(package_v1)
    second = service.build_index(package_v2)
    required = service.retrieve(_query(
        package_v1, query_text="unmatched", required_evidence_ids=["evidence_chunk_c", "evidence_chunk_a"], min_relevance=1.0,
    ), first)
    assert required.citation_ids() == ["evidence_chunk_a", "evidence_chunk_c"]
    assert [row.relevance for row in required.evidence_chunks] == [0.0, 0.0]
    empty = service.retrieve(_query(package_v1, query_text="unmatched"), first)
    assert empty.is_empty()
    tie = service.retrieve(_query(package_v1, query_text="rule", top_k=2), first)
    assert tie.citation_ids() == ["evidence_chunk_a", "evidence_chunk_b"]
    assert service.retrieve(_query(package_v2), second).index_checksum == second.checksum


def test_stub_keeps_empty_pgvector_surface_unchanged() -> None:
    service = M2EvidenceRetrievalServiceStub()
    ref = service.initialize_vector_store("course_1", NOW)
    assert ref.backend == "pgvector"
    assert ref.status == "empty"
    assert service.empty_retrieval_audit(ref, NOW).status == "empty"


def test_production_vector_surface_fails_closed_instead_of_returning_empty() -> None:
    service = M2EvidenceRetrievalService(
        Path("runtime/production-index"),
        "lexical",
        _MemoryArtifactRepository(),
        production=True,
    )

    with pytest.raises(DomainError) as index_error:
        service.build_vector_index(_package())
    assert index_error.value.code == "VECTOR_STORE_UNAVAILABLE"

    ref = EvidenceIndexRef(
        index_id="index-production",
        course_package_id="package_1",
        course_package_checksum=None,
        index_version="v1",
        storage_ref="pgvector:index-production",
        backend="pgvector",
        embedding_model_id=None,
        source_count=0,
        chunk_count=0,
        built_at=NOW,
        checksum="a" * 64,
        status="ready",
    )
    with pytest.raises(DomainError) as audit_error:
        service.empty_retrieval_audit(ref, NOW)
    assert audit_error.value.code == "RETRIEVAL_AUDIT_UNAVAILABLE"


def test_vector_provider_failure_is_mapped_and_audited() -> None:
    package = _package()
    provider = DeterministicEmbeddingProvider.for_tests(
        model_ref=EmbeddingModelIdentity(
            provider="test",
            model_name="failure-test",
            model_version="v1",
            dimension=4,
        )
    )
    audits = InMemoryRetrievalAuditStore()
    service = M2EvidenceRetrievalService(
        Path("runtime/vector-failure"),
        "lexical",
        _MemoryArtifactRepository(),
        embedding_provider=provider,
        vector_store=InMemoryVectorStore(),
        audit_store=audits,
        production=True,
    )
    index = service.build_vector_index(package)

    class _FailingProvider:
        model_ref = provider.model_ref

        def embed_query(self, text: str) -> tuple[float, ...]:
            del text
            raise RuntimeError("provider secret must not escape")

        def embed_documents(self, texts: object) -> tuple[tuple[float, ...], ...]:
            del texts
            raise RuntimeError("provider secret must not escape")

    service._embedding_provider = _FailingProvider()  # noqa: SLF001
    with pytest.raises(DomainError) as captured:
        service.retrieve_with_policy(
            _query(package),
            index,
            RetrievalPolicy(
                policy_id="provider-failure",
                strategy="vector",
                top_k=1,
                lexical_weight=0.0,
                vector_weight=1.0,
                rerank=False,
            ),
        )
    assert captured.value.code == "EMBEDDING_PROVIDER_UNAVAILABLE"
    assert len(audits._audits) == 1  # noqa: SLF001
    assert "provider secret" not in str(captured.value)


def test_memory_repository_package_only_save_fails_closed_and_is_not_readable() -> None:
    package = _package()
    snapshot = compile_snapshot(package)
    index = _service(_MemoryArtifactRepository()).build_index(package)
    repository = _MemoryM2Repository()

    with pytest.raises(DomainError) as captured:
        repository.save_index(index)

    assert captured.value.code == "INDEX_ARTIFACT_INVALID"
    assert captured.value.details == {}
    assert repository.get_index(index.index_id, index.index_version) is None
    assert repository.load_index_artifact(index.index_id, index.index_version) is None
    assert snapshot.checksum == index.checksum


@pytest.mark.parametrize(
    "update",
    [
        {"checksum": "f" * 64},
        {"course_package_checksum": "f" * 64},
        {"chunk_count": 99},
        {"source_count": 99},
        {"storage_ref": "lexical:different"},
        {"status": "failed"},
    ],
)
def test_memory_repository_revalidates_complete_ref_snapshot_bindings(
    update: dict[str, object],
) -> None:
    package = _package()
    snapshot = compile_snapshot(package)
    valid = _service(_MemoryArtifactRepository()).build_index(package)
    forged = valid.model_copy(update=update)
    repository = _MemoryM2Repository()

    with pytest.raises(DomainError) as captured:
        repository.save_index_artifact(forged, snapshot)

    assert captured.value.code == "INDEX_ARTIFACT_INVALID"
    assert captured.value.details == {}
    assert repository.load_index_artifact(valid.index_id, valid.index_version) is None


def test_memory_repository_load_is_deeply_isolated() -> None:
    package = _package()
    snapshot = compile_snapshot(package)
    index = _service(_MemoryArtifactRepository()).build_index(package)
    repository = _MemoryM2Repository()
    repository.save_index_artifact(index, snapshot)

    first_ref, first_snapshot = repository.load_index_artifact(
        index.index_id, index.index_version
    ) or pytest.fail("artifact missing")
    object.__setattr__(first_ref, "checksum", "f" * 64)
    object.__setattr__(first_snapshot, "checksum", "f" * 64)
    second_ref, second_snapshot = repository.load_index_artifact(
        index.index_id, index.index_version
    ) or pytest.fail("artifact missing")

    assert second_ref == index
    assert second_snapshot == snapshot


def test_memory_repository_conflicts_only_after_both_artifacts_are_valid() -> None:
    first_package = _package()
    second_package = _package(changed=True)
    first_snapshot = compile_snapshot(first_package)
    second_snapshot = compile_snapshot(second_package)
    first_index = _service(_MemoryArtifactRepository()).build_index(first_package)
    second_index = _service(_MemoryArtifactRepository()).build_index(second_package)
    repository = _MemoryM2Repository()
    repository.save_index_artifact(first_index, first_snapshot)

    with pytest.raises(DomainError) as captured:
        repository.save_index_artifact(second_index, second_snapshot)

    assert captured.value.code == "INDEX_VERSION_CONFLICT"
    assert captured.value.details == {}


def test_supplement_min_relevance_compares_after_12_place_rounding() -> None:
    package = _package()
    replacements = {
        "chunk_a": "alpha beta",
        "chunk_b": "alpha",
        "chunk_c": "unmatched",
    }
    chunks = [
        chunk.model_copy(
            update={
                "text": replacements[chunk.chunk_id],
                "sha256": hashlib.sha256(
                    replacements[chunk.chunk_id].encode("utf-8")
                ).hexdigest(),
            }
        )
        for chunk in package.content_chunks
    ]
    package = package.model_copy(update={"content_chunks": chunks})
    package = package.model_copy(update={"checksum": package.recalculate_checksum()})
    service = _service(_MemoryArtifactRepository())
    index = service.build_index(package)

    equal = service.retrieve(
        _query(package, query_text="alpha beta", min_relevance=0.444444444444),
        index,
    )
    above = service.retrieve(
        _query(package, query_text="alpha beta", min_relevance=0.444444444445),
        index,
    )

    assert equal.citation_ids() == ["evidence_chunk_a", "evidence_chunk_b"]
    assert above.citation_ids() == ["evidence_chunk_a"]
