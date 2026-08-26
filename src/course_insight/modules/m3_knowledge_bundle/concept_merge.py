"""Deterministic concept merge with lossless provenance set union."""

from __future__ import annotations

import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Sequence

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge_ingestion import (
    KnowledgeCandidate,
    KnowledgeEvidenceRef,
    MergedKnowledgeConcept,
)


def merge_knowledge_candidates(
    course_id: str,
    candidates: Sequence[KnowledgeCandidate],
) -> tuple[MergedKnowledgeConcept, ...]:
    """Merge exact normalized concept names and union every source reference."""

    if not isinstance(course_id, str) or not course_id.strip():
        raise ValueError("course_id must be non-empty")
    grouped: dict[str, list[KnowledgeCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[_normalized(candidate.name)].append(candidate)

    concepts: list[MergedKnowledgeConcept] = []
    for normalized_name in sorted(grouped):
        group = grouped[normalized_name]
        name = min((candidate.name for candidate in group), key=lambda value: (value.casefold(), value))
        description = max(
            (candidate.description for candidate in group),
            key=lambda value: (len(value), value),
        )
        aliases = _merge_aliases(group, name)
        evidence = _merge_evidence(group)
        concept_seed = f"{course_id}\0{normalized_name}"
        concepts.append(
            MergedKnowledgeConcept(
                concept_id=(
                    "concept_"
                    + hashlib.sha256(concept_seed.encode("utf-8")).hexdigest()
                ),
                course_id=course_id,
                name=name,
                description=description,
                aliases=aliases,
                evidence=evidence,
            )
        )
    return tuple(concepts)


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _merge_aliases(group: list[KnowledgeCandidate], name: str) -> list[str]:
    canonical = _normalized(name)
    representatives: dict[str, str] = {}
    for candidate in group:
        for alias in [candidate.name, *candidate.aliases]:
            normalized = _normalized(alias)
            if normalized and normalized != canonical:
                previous = representatives.get(normalized)
                if previous is None or (alias.casefold(), alias) < (
                    previous.casefold(),
                    previous,
                ):
                    representatives[normalized] = alias
    return [representatives[key] for key in sorted(representatives)]


def _merge_evidence(group: list[KnowledgeCandidate]) -> list[KnowledgeEvidenceRef]:
    references: dict[tuple[str, str, int, int, str], KnowledgeEvidenceRef] = {}
    for candidate in group:
        for reference in candidate.evidence:
            identity = reference.identity()
            existing = references.get(identity)
            if existing is not None and (
                existing.source_id != reference.source_id
                or existing.locator != reference.locator
            ):
                raise DomainError(
                    code="KNOWLEDGE_PROVENANCE_COLLISION",
                    module="m3",
                    message="one provenance identity resolves to conflicting source metadata",
                )
            references[identity] = reference
    return [references[identity] for identity in sorted(references)]
