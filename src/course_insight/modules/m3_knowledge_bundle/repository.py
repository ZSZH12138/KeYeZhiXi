"""M3 repository boundary for versioned knowledge bundles."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.knowledge import KnowledgeBundle


_BUNDLE_TABLE = "m3_knowledge_bundles"


class M3Repository(Protocol):
    """Persistence operations owned exclusively by M3."""

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        """Persist one knowledge-bundle version."""

    def get_knowledge_bundle(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> KnowledgeBundle | None:
        """Load one exact knowledge-bundle version."""
