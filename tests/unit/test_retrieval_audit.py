from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import RetrievalPolicy
from course_insight.modules.m2_evidence_retrieval.audit import (
    InMemoryRetrievalAuditStore,
    create_retrieval_audit,
    persist_retrieval_audit,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _query() -> EvidenceQuery:
    return EvidenceQuery(
        query_id="query-1",
        course_package_id="course-1",
        query_text="linear equation",
        concept_ids=["concept-1"],
        required_evidence_ids=[],
        item_id=None,
        use_case="qa",
        top_k=2,
        min_relevance=0.0,
    )


def _index() -> EvidenceIndexRef:
    return EvidenceIndexRef(
        index_id="index-1",
        course_package_id="course-1",
        course_package_checksum="a" * 64,
        index_version="v1",
        storage_ref="lexical:index-1",
        backend="lexical",
        embedding_model_id=None,
        source_count=1,
        chunk_count=2,
        built_at=NOW,
        checksum="b" * 64,
        status="ready",
    )


def _policy() -> RetrievalPolicy:
    return RetrievalPolicy(
        policy_id="policy-1",
        strategy="lexical",
        top_k=2,
        lexical_weight=1.0,
        vector_weight=0.0,
        rerank=False,
    )


def test_audit_hashes_query_and_excludes_raw_text_and_paths() -> None:
    envelope = create_retrieval_audit(
        query=_query(),
        index=_index(),
        policy=_policy(),
        status="succeeded",
        evidence_ids=["evidence-1"],
        scores=[0.75],
        latency_ms=12,
        request_id="request-1",
        created_at=NOW,
    )
    assert envelope.audit.audit_id
    assert envelope.metadata.query_checksum
    assert "linear equation" not in envelope.to_payload_text()
    assert "C:\\" not in envelope.to_payload_text()
    assert envelope.metadata.retrieved_scores == (0.75,)


def test_same_semantics_have_same_audit_identity_and_store_is_idempotent() -> None:
    first = create_retrieval_audit(
        query=_query(), index=_index(), policy=_policy(), status="empty",
        evidence_ids=[], scores=[], latency_ms=1, request_id="request-1", created_at=NOW,
    )
    second = create_retrieval_audit(
        query=_query(), index=_index(), policy=_policy(), status="empty",
        evidence_ids=[], scores=[], latency_ms=1, request_id="request-1", created_at=NOW,
    )
    assert first == second
    store = InMemoryRetrievalAuditStore()
    assert persist_retrieval_audit(store, first) == first
    assert persist_retrieval_audit(store, second) == first


def test_conflicting_same_identity_fails_closed_and_score_bindings_are_checked() -> None:
    first = create_retrieval_audit(
        query=_query(), index=_index(), policy=_policy(), status="succeeded",
        evidence_ids=["evidence-1"], scores=[0.7], latency_ms=1,
        request_id="request-1", created_at=NOW,
    )
    store = InMemoryRetrievalAuditStore()
    persist_retrieval_audit(store, first)
    conflict = first.with_latency(2)
    with pytest.raises(DomainError, match="audit identity conflict"):
        persist_retrieval_audit(store, conflict)
    with pytest.raises(DomainError, match="audit metadata is invalid"):
        create_retrieval_audit(
            query=_query(), index=_index(), policy=_policy(), status="succeeded",
            evidence_ids=["evidence-1"], scores=[], latency_ms=1,
            request_id="request-1", created_at=NOW,
        )
