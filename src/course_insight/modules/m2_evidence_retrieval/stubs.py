"""Deterministic local lexical M2 service stub."""

from pathlib import Path

from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)


class _MemoryM2Repository:
    def __init__(self) -> None:
        self.indexes: dict[tuple[str, str], EvidenceIndexRef] = {}

    def save_index(self, index: EvidenceIndexRef) -> None:
        self.indexes[(index.index_id, index.index_version)] = index

    def get_index(
        self,
        index_id: str,
        index_version: str,
    ) -> EvidenceIndexRef | None:
        return self.indexes.get((index_id, index_version))


class M2EvidenceRetrievalServiceStub(M2EvidenceRetrievalService):
    """Instantiate M2 with fixed local placeholder dependencies."""

    def __init__(self, index_dir: Path | None = None) -> None:
        repository: M2Repository = _MemoryM2Repository()
        super().__init__(
            index_dir or Path("runtime/stub/index"),
            "lexical",
            repository,
        )
