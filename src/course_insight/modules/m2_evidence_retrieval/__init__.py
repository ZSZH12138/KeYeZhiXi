"""M2 evidence-retrieval module."""

from course_insight.modules.m2_evidence_retrieval.repository import M2Repository
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m2_evidence_retrieval.stubs import (
    M2EvidenceRetrievalServiceStub,
)

__all__ = [
    "M2EvidenceRetrievalService",
    "M2EvidenceRetrievalServiceStub",
    "M2Repository",
]
