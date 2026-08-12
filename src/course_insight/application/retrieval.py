"""Application-level retrieval dispatch for auditable M2 calls."""

from __future__ import annotations

from typing import Any

from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.intelligence import RetrievalPolicy


def retrieve_for_application(
    m2_service: Any,
    evidence_query: EvidenceQuery,
    evidence_index_ref: EvidenceIndexRef,
    *,
    request_id: str | None = None,
) -> Any:
    """Use the formal M2 policy boundary while keeping legacy test doubles usable.

    The application baseline is deliberately lexical until a caller supplies a
    vector-backed index.  The M2 service still owns all strategy validation and
    audit persistence; this helper only prevents application code from bypassing
    that boundary.
    """

    retrieve_with_policy = getattr(m2_service, "retrieve_with_policy", None)
    if callable(retrieve_with_policy):
        policy = RetrievalPolicy(
            policy_id="application-lexical-v1",
            strategy="lexical",
            top_k=evidence_query.top_k,
            lexical_weight=1.0,
            vector_weight=0.0,
            rerank=False,
        )
        return retrieve_with_policy(
            evidence_query,
            evidence_index_ref,
            policy,
            request_id=request_id or evidence_query.query_id,
        )
    legacy_retrieve = getattr(m2_service, "retrieve", None)
    if not callable(legacy_retrieve):
        raise TypeError("M2 retrieval service does not expose a supported entrypoint")
    return legacy_retrieve(
        evidence_query=evidence_query,
        evidence_index_ref=evidence_index_ref,
    )


__all__ = ["retrieve_for_application"]
