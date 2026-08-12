from __future__ import annotations

from pathlib import Path

from course_insight.application.persistence import BackendReadiness
from course_insight.application.readiness import evaluate_capability_readiness
from course_insight.modules.m1_course_governance.parsers import (
    default_parser_registry,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    InMemoryRetrievalAuditStore,
)
from course_insight.modules.m2_evidence_retrieval.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingModelIdentity,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    InMemoryVectorStore,
)
from course_insight.modules.m3_knowledge_bundle.service import (
    M3KnowledgeBundleService,
)
from course_insight.modules.m3_knowledge_bundle.stubs import _MemoryM3Repository
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    InMemoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)


class _ReadyBackend:
    backend_name = "postgresql"

    def readiness(self) -> BackendReadiness:
        return BackendReadiness(backend="postgresql", ready=True)


class _UnavailableBackend:
    backend_name = "postgresql"

    def readiness(self) -> BackendReadiness:
        return BackendReadiness(
            backend="postgresql", ready=False, reason="schema_unavailable"
        )


def _m2(*, configured: bool) -> M2EvidenceRetrievalService:
    provider = None
    vector_store = None
    audit_store = None
    if configured:
        provider = DeterministicEmbeddingProvider.for_tests(
            model_ref=EmbeddingModelIdentity(
                provider="test",
                model_name="readiness",
                model_version="v1",
                dimension=4,
            )
        )
        vector_store = InMemoryVectorStore()
        audit_store = InMemoryRetrievalAuditStore()
    return M2EvidenceRetrievalService(
        Path("runtime/readiness"),
        "lexical",
        _MemoryM3Repository(),  # type: ignore[arg-type]
        embedding_provider=provider,
        vector_store=vector_store,
        audit_store=audit_store,
        production=True,
    )


def test_production_readiness_fails_closed_when_s1_s4_dependencies_are_missing() -> None:
    result = evaluate_capability_readiness(
        backend=_UnavailableBackend(),
        parser_registry=default_parser_registry(),
        m2_service=_m2(configured=False),
        m3_service=M3KnowledgeBundleService(
            _MemoryM3Repository(),
            None,
            require_teacher_approval=True,
        ),
        production=True,
    )

    assert result["status"] == "unavailable"
    assert result["backend"] == "unavailable"
    assert result["parser"] == "ok"
    assert result["embedding"] == "unavailable"
    assert result["vector_store"] == "unavailable"
    assert result["retrieval_audit"] == "unavailable"
    assert result["teacher_review"] == "unavailable"


def test_production_readiness_is_ready_when_all_s1_s4_ports_are_bound() -> None:
    result = evaluate_capability_readiness(
        backend=_ReadyBackend(),
        parser_registry=default_parser_registry(),
        m2_service=_m2(configured=True),
        m3_service=M3KnowledgeBundleService(
            _MemoryM3Repository(),
            None,
            review_workflow=TeacherReviewWorkflow(
                InMemoryTeacherReviewRepository()
            ),
            require_teacher_approval=True,
        ),
        production=True,
    )

    assert result == {
        "status": "ready",
        "backend": "ok",
        "parser": "ok",
        "embedding": "ok",
        "vector_store": "ok",
        "retrieval_audit": "ok",
        "teacher_review": "ok",
    }
