"""Privacy-safe, deterministic retrieval audit envelopes and persistence port."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import RetrievalAudit, RetrievalPolicy
from course_insight.infrastructure.json_io import dumps_json


AuditStatus = Literal["empty", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class RetrievalAuditMetadata:
    """Safe optional data kept outside the frozen public contract schema."""

    query_checksum: str
    index_version: str
    policy_version: str
    embedding_model_id: str | None
    retrieved_scores: tuple[float, ...]
    latency_ms: int
    request_id: str


@dataclass(frozen=True, slots=True)
class RetrievalAuditEnvelope:
    """Public audit identity plus redacted internal metadata."""

    audit: RetrievalAudit
    metadata: RetrievalAuditMetadata

    def with_latency(self, latency_ms: int) -> "RetrievalAuditEnvelope":
        _validate_latency(latency_ms)
        return replace(
            self,
            metadata=replace(self.metadata, latency_ms=latency_ms),
        )

    def to_payload_text(self) -> str:
        """Serialize only redacted fields for persistence/log assertions."""

        return dumps_json(
            {
                "audit": self.audit.model_dump(mode="json"),
                "metadata": {
                    "embedding_model_id": self.metadata.embedding_model_id,
                    "index_version": self.metadata.index_version,
                    "latency_ms": self.metadata.latency_ms,
                    "policy_version": self.metadata.policy_version,
                    "query_checksum": self.metadata.query_checksum,
                    "request_id": self.metadata.request_id,
                    "retrieved_scores": list(self.metadata.retrieved_scores),
                },
            }
        )

    @classmethod
    def from_payload_text(cls, payload: str | bytes) -> "RetrievalAuditEnvelope":
        """Decode the canonical redacted payload used by database adapters."""

        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if type(payload) is not bytes:
            raise _invalid()
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise _invalid() from None
        if type(value) is not dict or set(value) != {"audit", "metadata"}:
            raise _invalid()
        metadata = value["metadata"]
        if type(metadata) is not dict or set(metadata) != {
            "embedding_model_id", "index_version", "latency_ms", "policy_version",
            "query_checksum", "request_id", "retrieved_scores",
        }:
            raise _invalid()
        try:
            envelope = cls(
                audit=RetrievalAudit.model_validate(value["audit"]),
                metadata=RetrievalAuditMetadata(
                    query_checksum=metadata["query_checksum"],
                    index_version=metadata["index_version"],
                    policy_version=metadata["policy_version"],
                    embedding_model_id=metadata["embedding_model_id"],
                    retrieved_scores=tuple(metadata["retrieved_scores"]),
                    latency_ms=metadata["latency_ms"],
                    request_id=metadata["request_id"],
                ),
            )
        except (TypeError, ValueError):
            raise _invalid() from None
        if envelope.to_payload_text().encode("utf-8") != payload:
            raise _invalid()
        _validate_decoded_envelope(envelope)
        return envelope


class RetrievalAuditStore(Protocol):
    """Persistence port for idempotent audit envelopes."""

    def get(self, audit_id: str) -> RetrievalAuditEnvelope | None:
        """Return an existing audit by immutable identity."""

    def save(self, envelope: RetrievalAuditEnvelope) -> None:
        """Insert one envelope or reject a conflicting identity."""


class RepositoryRetrievalAuditStore:
    """Adapt an M2/S5 repository's explicit audit methods to the audit port."""

    def __init__(self, repository: object) -> None:
        self._repository = repository

    def get(self, audit_id: str) -> RetrievalAuditEnvelope | None:
        method = getattr(self._repository, "load_retrieval_audit", None)
        if not callable(method):
            raise DomainError(
                code="RETRIEVAL_AUDIT_PERSISTENCE_FAILED",
                module="m2",
                message="retrieval audit store is unavailable",
            )
        return method(audit_id)

    def save(self, envelope: RetrievalAuditEnvelope) -> None:
        method = getattr(self._repository, "save_retrieval_audit", None)
        if not callable(method):
            raise DomainError(
                code="RETRIEVAL_AUDIT_PERSISTENCE_FAILED",
                module="m2",
                message="retrieval audit store is unavailable",
            )
        method(envelope)


class InMemoryRetrievalAuditStore:
    """Deterministic test adapter; production adapters live in infrastructure."""

    def __init__(self) -> None:
        self._audits: dict[str, RetrievalAuditEnvelope] = {}

    def get(self, audit_id: str) -> RetrievalAuditEnvelope | None:
        return self._audits.get(audit_id)

    def save(self, envelope: RetrievalAuditEnvelope) -> None:
        existing = self._audits.get(envelope.audit.audit_id)
        if existing is not None and existing != envelope:
            raise DomainError(
                code="RETRIEVAL_AUDIT_CONFLICT",
                module="m2",
                message="audit identity conflict",
                recoverable=False,
            )
        self._audits[envelope.audit.audit_id] = envelope


def create_retrieval_audit(
    *,
    query: EvidenceQuery,
    index: EvidenceIndexRef,
    policy: RetrievalPolicy,
    status: AuditStatus,
    evidence_ids: Sequence[str],
    scores: Sequence[float],
    latency_ms: int,
    request_id: str,
    created_at: datetime,
    model_id: str | None = None,
) -> RetrievalAuditEnvelope:
    """Build an audit without retaining raw query or document contents."""

    _validate_inputs(
        query=query,
        index=index,
        policy=policy,
        status=status,
        evidence_ids=evidence_ids,
        scores=scores,
        latency_ms=latency_ms,
        request_id=request_id,
        created_at=created_at,
    )
    query_checksum = query.cache_key()
    model_identity = model_id if model_id is not None else index.embedding_model_id
    identity_payload = {
        "index_checksum": index.checksum,
        "index_id": index.index_id,
        "index_version": index.index_version,
        "model_id": model_identity,
        "policy_id": policy.policy_id,
        "policy": policy.model_dump(mode="json"),
        "query_checksum": query_checksum,
        "request_id": request_id,
        "status": status,
        "evidence_ids": list(evidence_ids),
    }
    audit_id = hashlib.sha256(
        dumps_json(identity_payload).encode("utf-8")
    ).hexdigest()
    audit = RetrievalAudit(
        audit_id=audit_id,
        query_id=query.query_id,
        index_id=index.index_id,
        policy_id=policy.policy_id,
        retrieved_evidence_ids=list(evidence_ids),
        status=status,
        created_at=created_at,
    )
    metadata = RetrievalAuditMetadata(
        query_checksum=query_checksum,
        index_version=index.index_version,
        policy_version=policy.schema_version,
        embedding_model_id=model_identity,
        retrieved_scores=tuple(float(score) for score in scores),
        latency_ms=latency_ms,
        request_id=request_id,
    )
    envelope = RetrievalAuditEnvelope(audit=audit, metadata=metadata)
    _validate_decoded_envelope(envelope)
    return envelope


def persist_retrieval_audit(
    store: RetrievalAuditStore, envelope: RetrievalAuditEnvelope
) -> RetrievalAuditEnvelope:
    """Persist safely and return the canonical stored value on replay."""

    existing = store.get(envelope.audit.audit_id)
    if existing is not None:
        if existing != envelope:
            raise DomainError(
                code="RETRIEVAL_AUDIT_CONFLICT",
                module="m2",
                message="audit identity conflict",
            )
        return existing
    store.save(envelope)
    stored = store.get(envelope.audit.audit_id)
    if stored is None or stored != envelope:
        raise DomainError(
            code="RETRIEVAL_AUDIT_PERSISTENCE_FAILED",
            module="m2",
            message="retrieval audit could not be verified",
        )
    return stored


def _validate_inputs(
    *,
    query: EvidenceQuery,
    index: EvidenceIndexRef,
    policy: RetrievalPolicy,
    status: AuditStatus,
    evidence_ids: Sequence[str],
    scores: Sequence[float],
    latency_ms: int,
    request_id: str,
    created_at: datetime,
) -> None:
    if not isinstance(query, EvidenceQuery) or not isinstance(index, EvidenceIndexRef):
        raise _invalid()
    if not isinstance(policy, RetrievalPolicy) or status not in {"empty", "succeeded", "failed"}:
        raise _invalid()
    if index.status != "ready":
        raise _invalid()
    if not isinstance(request_id, str) or not request_id.strip():
        raise _invalid()
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise _invalid()
    if type(evidence_ids) not in {list, tuple} or len(set(evidence_ids)) != len(evidence_ids):
        raise _invalid()
    if any(not isinstance(value, str) or not value.strip() for value in evidence_ids):
        raise _invalid()
    if type(scores) not in {list, tuple} or len(scores) != len(evidence_ids):
        raise _invalid()
    if any(type(score) not in {int, float} or not math.isfinite(float(score)) or not 0.0 <= float(score) <= 1.0 for score in scores):
        raise _invalid()
    if status == "empty" and evidence_ids:
        raise _invalid()
    _validate_latency(latency_ms)


def _validate_latency(latency_ms: int) -> None:
    if type(latency_ms) is not int or latency_ms < 0:
        raise _invalid()


def _validate_decoded_envelope(envelope: RetrievalAuditEnvelope) -> None:
    metadata = envelope.metadata
    if (
        not isinstance(metadata.query_checksum, str)
        or len(metadata.query_checksum) != 64
        or any(character not in "0123456789abcdef" for character in metadata.query_checksum)
        or not isinstance(metadata.index_version, str)
        or not metadata.index_version.strip()
        or not isinstance(metadata.policy_version, str)
        or not metadata.policy_version.strip()
        or not isinstance(metadata.request_id, str)
        or not metadata.request_id.strip()
        or type(metadata.retrieved_scores) is not tuple
        or len(metadata.retrieved_scores) != len(envelope.audit.retrieved_evidence_ids)
    ):
        raise _invalid()
    _validate_latency(metadata.latency_ms)
    if any(
        type(score) not in {int, float}
        or not math.isfinite(float(score))
        or not 0.0 <= float(score) <= 1.0
        for score in metadata.retrieved_scores
    ):
        raise _invalid()


def _invalid() -> DomainError:
    return DomainError(
        code="RETRIEVAL_AUDIT_INVALID",
        module="m2",
        message="audit metadata is invalid",
    )


__all__ = [
    "InMemoryRetrievalAuditStore",
    "RetrievalAuditEnvelope",
    "RetrievalAuditMetadata",
    "RepositoryRetrievalAuditStore",
    "RetrievalAuditStore",
    "create_retrieval_audit",
    "persist_retrieval_audit",
]
