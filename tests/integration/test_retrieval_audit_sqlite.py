from __future__ import annotations

from datetime import datetime, timezone

from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.intelligence import RetrievalPolicy
from course_insight.modules.m2_evidence_retrieval.audit import (
    RepositoryRetrievalAuditStore,
    create_retrieval_audit,
    persist_retrieval_audit,
)
from course_insight.infrastructure.sqlite.m1_m2_m3_repository import (
    SQLiteM1M2M3Repository,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_sqlite_repository_restores_redacted_retrieval_audit(tmp_path) -> None:
    repository = SQLiteM1M2M3Repository(tmp_path / "runtime.sqlite")
    repository.initialize()
    query = EvidenceQuery(
        query_id="query-1",
        course_package_id="course-1",
        query_text="secret source text",
        concept_ids=[],
        required_evidence_ids=[],
        item_id=None,
        use_case="qa",
        top_k=1,
        min_relevance=0.0,
    )
    index = EvidenceIndexRef(
        index_id="index-1",
        course_package_id="course-1",
        course_package_checksum="a" * 64,
        index_version="v1",
        storage_ref="lexical:index-1",
        backend="lexical",
        embedding_model_id=None,
        source_count=1,
        chunk_count=1,
        built_at=NOW,
        checksum="b" * 64,
        status="ready",
    )
    policy = RetrievalPolicy(
        policy_id="policy-1",
        strategy="lexical",
        top_k=1,
        lexical_weight=1.0,
        vector_weight=0.0,
        rerank=False,
    )
    envelope = create_retrieval_audit(
        query=query,
        index=index,
        policy=policy,
        status="empty",
        evidence_ids=[],
        scores=[],
        latency_ms=5,
        request_id="request-1",
        created_at=NOW,
    )
    store = RepositoryRetrievalAuditStore(repository)
    assert persist_retrieval_audit(store, envelope) == envelope
    assert repository.load_retrieval_audit(envelope.audit.audit_id) == envelope
    assert "secret source text" not in str(repository.export_manifest())
