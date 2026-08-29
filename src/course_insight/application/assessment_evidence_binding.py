"""Narrow evidence-identity compatibility for release-backed rubric scoring."""

from __future__ import annotations

import re

from course_insight.contracts.assessment import RubricScoringTask
from course_insight.contracts.evidence import EvidenceBundle


_LEGACY_RELEASE_EVIDENCE_ID = re.compile(r"^evidence-[1-9][0-9]*$")


def bind_release_rubric_evidence(
    task: RubricScoringTask,
    evidence: EvidenceBundle,
) -> RubricScoringTask:
    """Bind one legacy release rubric to its aligned retrieved evidence.

    Old release projections stored database-reference evidence IDs, while M2
    exposes canonical chunk-derived IDs. Only the single-criterion release
    rubric shape is eligible. Same-concept chunks are preferred; when legacy
    source chunks lack concept labels, the exact query-aligned retrieval is the
    conservative fallback. Every other mismatch remains unchanged so M7 keeps
    failing closed.
    """

    if evidence.query_id != task.evidence_query_id or evidence.is_empty():
        return task
    available_ids = set(evidence.citation_ids())
    criteria = task.rubric.criteria
    if all(
        set(criterion.course_evidence_ids).intersection(available_ids)
        for criterion in criteria
    ):
        return task
    if (
        len(criteria) != 1
        or not criteria[0].course_evidence_ids
        or not all(
            _LEGACY_RELEASE_EVIDENCE_ID.fullmatch(evidence_id)
            for evidence_id in criteria[0].course_evidence_ids
        )
    ):
        return task

    target_concepts = set(task.item_instance.concept_ids)
    same_concept_ids = list(
        dict.fromkeys(
            chunk.evidence_id
            for chunk in evidence.evidence_chunks
            if target_concepts.intersection(chunk.concept_ids)
        )
    )
    eligible_ids = same_concept_ids or list(
        dict.fromkeys(chunk.evidence_id for chunk in evidence.evidence_chunks)
    )

    criterion = criteria[0].model_copy(
        update={"course_evidence_ids": eligible_ids},
        deep=True,
    )
    rubric = task.rubric.model_copy(
        update={"criteria": [criterion]},
        deep=True,
    )
    bound = task.model_copy(update={"rubric": rubric}, deep=True)
    bound.validate_business_rules()
    return bound


__all__ = ["bind_release_rubric_evidence"]
