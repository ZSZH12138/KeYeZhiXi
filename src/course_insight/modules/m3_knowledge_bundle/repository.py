"""M3 repository boundary for versioned knowledge bundles."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationReport,
)


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

    def save_bundle_artifact(
        self,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        """Persist one complete approved publication artifact."""

    def load_bundle_artifact(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> tuple[KnowledgeBundle, M3ValidationReport, M3SeedSnapshot] | None:
        """Load one exact complete approved publication artifact."""

    def save_rejected_validation(
        self,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        """Persist one rejected validation result without a bundle."""

    def load_rejected_validation(
        self,
        course_package_id: str,
        report_id: str,
    ) -> tuple[M3ValidationReport, M3SeedSnapshot] | None:
        """Load one exact rejected validation result."""
