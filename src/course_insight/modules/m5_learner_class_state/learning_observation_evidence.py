"""Authoritative audit identity rules for immutable learning observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import TypeAlias

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    ConceptResponse,
    LearningObservation,
)


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


def select_current_learning_observations(
    observations: Iterable[LearningObservation],
) -> list[LearningObservation]:
    """Retain history while exposing only the latest version of each audit."""

    normalized = normalize_learning_observations(observations)
    latest_versions = {
        audit_id: max(
            observation.source_audit_version
            for observation in normalized
            if observation.source_audit_id == audit_id
        )
        for audit_id in {
            observation.source_audit_id for observation in normalized
        }
    }
    return [
        observation
        for observation in normalized
        if observation.source_audit_version
        == latest_versions[observation.source_audit_id]
    ]


def concept_response_evidence_checksum(response: ConceptResponse) -> str:
    """Hash governed BKT evidence while ignoring its projection ID."""

    payload = response.model_dump(
        mode="json",
        exclude={"observation_id"},
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_concept_responses(
    responses: Iterable[ConceptResponse],
) -> list[ConceptResponse]:
    """Consume one BKT audit version once and reject contradictions."""

    governed: list[ConceptResponse] = []
    seen: dict[AuditKey, str] = {}
    for response in responses:
        audit_key = (response.source_audit_id, response.source_audit_version)
        evidence_checksum = concept_response_evidence_checksum(response)
        previous_checksum = seen.get(audit_key)
        if previous_checksum is None:
            seen[audit_key] = evidence_checksum
            governed.append(response)
            continue
        if previous_checksum != evidence_checksum:
            raise DomainError(
                code="LEARNING_OBSERVATION_AUDIT_CONFLICT",
                module="m5",
                message="one learning audit version has conflicting evidence",
                recoverable=True,
            )
    return governed


def select_current_concept_responses(
    responses: Iterable[ConceptResponse],
) -> list[ConceptResponse]:
    """Expose only the latest immutable BKT projection for each audit."""

    normalized = normalize_concept_responses(responses)
    latest_versions = {
        audit_id: max(
            response.source_audit_version
            for response in normalized
            if response.source_audit_id == audit_id
        )
        for audit_id in {response.source_audit_id for response in normalized}
    }
    return [
        response
        for response in normalized
        if response.source_audit_version
        == latest_versions[response.source_audit_id]
    ]


__all__ = [
    "AuditKey",
    "concept_response_evidence_checksum",
    "learning_observation_audit_key",
    "learning_observation_evidence_checksum",
    "normalize_concept_responses",
    "normalize_learning_observations",
    "select_current_concept_responses",
    "select_current_learning_observations",
]
