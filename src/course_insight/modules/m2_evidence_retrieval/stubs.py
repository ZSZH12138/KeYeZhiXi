"""Deterministic local lexical M2 service stub."""

from __future__ import annotations

from pathlib import Path

from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
    snapshot_from_payloads,
    snapshot_to_payloads,
)
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository
from course_insight.modules.m2_evidence_retrieval.service import M2EvidenceRetrievalService


def _artifact_error(code: str, message: str) -> DomainError:
    return DomainError(code=code, module="m2", message=message, details={})


class _MemoryM2Repository:
    """Complete in-memory immutable-artifact analogue for the M2 stub."""

    def __init__(self) -> None:
        self._artifacts: dict[tuple[str, str], tuple[EvidenceIndexRef, LexicalIndexSnapshot]] = {}

    def save_index(self, index: EvidenceIndexRef) -> None:
        del index
        raise _artifact_error(
            "INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid"
        )

    def get_index(self, index_id: str, index_version: str) -> EvidenceIndexRef | None:
        artifact = self.load_index_artifact(index_id, index_version)
        return None if artifact is None else artifact[0]

    @staticmethod
    def _copy_snapshot(snapshot: LexicalIndexSnapshot) -> LexicalIndexSnapshot:
        # Reparse canonical payloads to prevent aliases even if private objects are abused.
        return snapshot_from_payloads(snapshot_to_payloads(snapshot))

    @staticmethod
    def _validated_ref(
        index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> EvidenceIndexRef:
        if not isinstance(index, EvidenceIndexRef):
            raise ValueError("index is invalid")
        rebuilt = EvidenceIndexRef.model_validate(index.model_dump(mode="json"))
        if rebuilt != index or (
            rebuilt.status != "ready"
            or rebuilt.backend != "lexical"
            or rebuilt.embedding_model_id is not None
            or rebuilt.storage_ref != f"lexical:{rebuilt.index_id}"
            or rebuilt.course_package_checksum is None
            or rebuilt.course_package_id != snapshot.course_package_id
            or rebuilt.course_package_checksum != snapshot.course_package_checksum
            or rebuilt.checksum != snapshot.checksum
            or rebuilt.chunk_count != len(snapshot.documents)
            or rebuilt.source_count != len({row.source_id for row in snapshot.documents})
            or rebuilt.built_at.tzinfo is None
            or rebuilt.built_at.utcoffset() is None
        ):
            raise ValueError("index bindings are invalid")
        return rebuilt

    def save_index_artifact(
        self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> None:
        try:
            copied_snapshot = self._copy_snapshot(snapshot)
            copied_index = self._validated_ref(index, copied_snapshot)
            stored = (copied_index.model_copy(deep=True), copied_snapshot)
        except Exception:
            raise _artifact_error("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid") from None
        key = (index.index_id, index.index_version)
        current = self._artifacts.get(key)
        if current is not None:
            if (
                current[0].model_dump(mode="json")
                != stored[0].model_dump(mode="json")
                or current[1] != stored[1]
            ):
                raise _artifact_error(
                    "INDEX_VERSION_CONFLICT",
                    "evidence index version conflicts with an immutable artifact",
                )
            return
        self._artifacts[key] = stored

    def load_index_artifact(
        self, index_id: str, index_version: str
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        current = self._artifacts.get((index_id, index_version))
        if current is None:
            return None
        try:
            return current[0].model_copy(deep=True), self._copy_snapshot(current[1])
        except Exception:
            raise _artifact_error("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid") from None


class M2EvidenceRetrievalServiceStub(M2EvidenceRetrievalService):
    """Instantiate M2 with fixed local lexical dependencies and durable memory."""

    def __init__(self, index_dir: Path | None = None) -> None:
        repository: M2Repository = _MemoryM2Repository()
        super().__init__(index_dir or Path("runtime/stub/index"), "lexical", repository)
