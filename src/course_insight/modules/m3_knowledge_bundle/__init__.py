"""M3 knowledge-bundle module."""

from course_insight.modules.m3_knowledge_bundle.repository import M3Repository
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService
from course_insight.modules.m3_knowledge_bundle.stubs import (
    M3KnowledgeBundleServiceStub,
)

__all__ = [
    "M3KnowledgeBundleService",
    "M3KnowledgeBundleServiceStub",
    "M3Repository",
]
