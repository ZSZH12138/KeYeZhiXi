"""Authoritative audit identity rules for immutable learning observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import TypeAlias

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import LearningObservation


AuditKey: TypeAlias = tuple[str, int]


def learning_observation_audit_key(
    observation: LearningObservation,
) -> AuditKey:
    """Return the contract-owned identity of one authoritative audit version."""

    return observation.source_audit_id, observation.source_audit_version


def learning_observation_evidence_checksum(
    observation: LearningObservation,
) -> str:
    """Hash governed evidence while ignoring its replaceable projection ID."""

    payload = observation.model_dump(
        mode="json",
        exclude={"observation_id"},
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_learning_observations(
    observations: Iterable[LearningObservation],
) -> list[LearningObservation]:
    """Consume an audit version once and reject contradictory projections."""

    governed: list[LearningObservation] = []
    seen: dict[AuditKey, str] = {}
    for observation in observations:
        audit_key = learning_observation_audit_key(observation)
        evidence_checksum = learning_observation_evidence_checksum(observation)
        previous_checksum = seen.get(audit_key)
        if previous_checksum is None:
            seen[audit_key] = evidence_checksum
            governed.append(observation)
            continue
        if previous_checksum != evidence_checksum:
            raise DomainError(
                code="LEARNING_OBSERVATION_AUDIT_CONFLICT",
                module="m5",
                message="one learning audit version has conflicting evidence",
                recoverable=True,
            )
    return governed


__all__ = [
    "AuditKey",
    "learning_observation_audit_key",
    "learning_observation_evidence_checksum",
    "normalize_learning_observations",
]
