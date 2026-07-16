"""M2 repository boundary for course-evidence indexes."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.evidence import EvidenceIndexRef


_INDEX_TABLE = "m2_evidence_indexes"


class M2Repository(Protocol):
    """Persistence operations owned exclusively by M2."""

    def save_index(self, index: EvidenceIndexRef) -> None:
        """Persist one evidence-index version reference."""

    def get_index(
        self,
        index_id: str,
        index_version: str,
    ) -> EvidenceIndexRef | None:
        """Load one exact evidence-index version."""
