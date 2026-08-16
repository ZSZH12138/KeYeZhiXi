"""M2 repository boundary for course-evidence indexes."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.modules.m2_evidence_retrieval.lexical import LexicalIndexSnapshot


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

    def save_index_artifact(
        self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> None:
        """Persist one complete, immutable lexical index artifact."""

    def load_index_artifact(
        self, index_id: str, index_version: str
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        """Load one exact complete lexical index artifact."""
