"""Private immutable-file persistence for complete M2 lexical indexes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.runtime_artifacts import (
    ArtifactStoreError,
    ImmutableArtifactStore,
)
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
    snapshot_from_payloads,
    snapshot_to_payloads,
)


_MODULE = "m2"
_OBJECT_TYPE = "evidence_index"
_FORMAT = "m2_evidence_index/v1"
_REQUIRED_PAYLOADS = frozenset({"index_ref.json", "documents.json", "postings.json"})


def _invalid() -> DomainError:
    return DomainError(
        code="INDEX_ARTIFACT_INVALID", module="m2",
        message="evidence index artifact is invalid",
    )


def _version_conflict() -> DomainError:
    return DomainError(
        code="INDEX_VERSION_CONFLICT", module="m2",
        message="evidence index version conflicts with an immutable artifact",
    )


class FileM2Repository:
    """Persist only complete, checksum-bound M2 lexical indexes under runtime."""

    def __init__(self, runtime_dir: Path) -> None:
        self._store = ImmutableArtifactStore(runtime_dir)

    def save_index(self, index: EvidenceIndexRef) -> None:
        """Reject legacy package-only persistence: it cannot restore retrieval."""

        del index
        raise _invalid()

    def get_index(self, index_id: str, index_version: str) -> EvidenceIndexRef | None:
        loaded = self.load_index_artifact(index_id, index_version)
        return None if loaded is None else loaded[0]

    def save_index_artifact(
        self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> None:
        try:
            payloads, metadata = self._serialize(index, snapshot)
            self._store.publish(
                module=_MODULE,
                object_type=_OBJECT_TYPE,
                object_id=index.index_id,
                object_version=index.index_version,
                payloads=payloads,
                metadata=metadata,
            )
        except DomainError:
            raise
        except ArtifactStoreError as error:
            if error.reason == "version_conflict":
                raise _version_conflict() from None
            raise _invalid() from None
        except Exception:
            raise _invalid() from None

    def load_index_artifact(
        self, index_id: str, index_version: str
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        try:
            loaded = self._store.load(
                module=_MODULE,
                object_type=_OBJECT_TYPE,
                object_id=index_id,
                object_version=index_version,
            )
        except ArtifactStoreError as error:
            if error.reason == "missing_artifact":
                return None
            raise _invalid() from None
        try:
            return self._deserialize(loaded.payloads, loaded.metadata, index_id, index_version)
        except Exception:
            raise _invalid() from None

    @staticmethod
    def _sha256(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _decode_canonical_json(payload: bytes) -> dict[str, Any]:
        if type(payload) is not bytes:
            raise ValueError("payload must be bytes")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
        if type(value) is not dict or dumps_json(value).encode("utf-8") != payload:
            raise ValueError("payload is not canonical JSON")
        return value

    @staticmethod
    def _validate_ref(index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot) -> None:
        if not isinstance(index, EvidenceIndexRef):
            raise ValueError("index is invalid")
        # Revalidation closes model_copy(update=...) bypasses before publishing.
        rebuilt = EvidenceIndexRef.model_validate(index.model_dump(mode="json"))
        if rebuilt != index:
            raise ValueError("index is invalid")
        if (
            index.status != "ready"
            or index.backend != "lexical"
            or index.embedding_model_id is not None
            or index.storage_ref != f"lexical:{index.index_id}"
            or index.course_package_checksum is None
            or index.course_package_id != snapshot.course_package_id
            or index.course_package_checksum != snapshot.course_package_checksum
            or index.checksum != snapshot.checksum
            or index.chunk_count != len(snapshot.documents)
            or index.source_count != len({row.source_id for row in snapshot.documents})
            or index.built_at.tzinfo is None
            or index.built_at.utcoffset() is None
        ):
            raise ValueError("index bindings are invalid")

    def _serialize(
        self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> tuple[dict[str, bytes], dict[str, str]]:
        lexical_payloads = snapshot_to_payloads(snapshot)
        self._validate_ref(index, snapshot)
        payloads = {
            "index_ref.json": dumps_json(index.model_dump(mode="json")).encode("utf-8"),
            **lexical_payloads,
        }
        metadata = self._metadata(index, snapshot, payloads)
        return payloads, metadata

    def _deserialize(
        self,
        payloads: Mapping[str, bytes],
        metadata: Mapping[str, Any],
        index_id: str,
        index_version: str,
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot]:
        if not isinstance(payloads, Mapping) or set(payloads) != _REQUIRED_PAYLOADS:
            raise ValueError("payload names are invalid")
        ref_data = self._decode_canonical_json(payloads["index_ref.json"])
        index = EvidenceIndexRef.model_validate(ref_data)
        if dumps_json(index.model_dump(mode="json")).encode("utf-8") != payloads["index_ref.json"]:
            raise ValueError("index reference is not exact")
        snapshot_payloads = {
            "documents.json": payloads["documents.json"],
            "postings.json": payloads["postings.json"],
        }
        snapshot = snapshot_from_payloads(snapshot_payloads)
        if snapshot_to_payloads(snapshot) != snapshot_payloads:
            raise ValueError("snapshot is not exact")
        if index.index_id != index_id or index.index_version != index_version:
            raise ValueError("artifact identity differs from reference")
        self._validate_ref(index, snapshot)
        expected_metadata = self._metadata(index, snapshot, payloads)
        if not isinstance(metadata, Mapping) or dict(metadata) != expected_metadata:
            raise ValueError("artifact metadata is invalid")
        return index.model_copy(deep=True), snapshot

    def _metadata(
        self,
        index: EvidenceIndexRef,
        snapshot: LexicalIndexSnapshot,
        payloads: Mapping[str, bytes],
    ) -> dict[str, str]:
        if set(payloads) != _REQUIRED_PAYLOADS:
            raise ValueError("payload names are invalid")
        return {
            "format": _FORMAT,
            "course_package_id": index.course_package_id,
            "course_package_checksum": index.course_package_checksum or "",
            "index_checksum": index.checksum,
            "tokenizer_version": snapshot.tokenizer_version,
            "documents_sha256": self._sha256(payloads["documents.json"]),
            "postings_sha256": self._sha256(payloads["postings.json"]),
        }


__all__ = ["FileM2Repository"]
