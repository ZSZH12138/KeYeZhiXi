"""Evidence-supported, stable target selection for M6 tutoring actions."""

from __future__ import annotations

from collections.abc import Iterable

from course_insight.contracts.assessment import RemediationPlan
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import DiagnosisResult, LearnerStateSnapshot


def select_target_concept_ids(
    *,
    diagnosis_result: DiagnosisResult,
    remediation_plan: RemediationPlan,
    learner_state_snapshot: LearnerStateSnapshot,
    weak_mastery_threshold: float,
) -> list[str]:
    """Return diagnosed targets in stable source-priority order."""

    diagnosed_concept_ids = {
        concept_id
        for item_diagnosis in diagnosis_result.item_diagnoses
        for concept_id in item_diagnosis.concept_ids
    }
    candidate_sources: tuple[Iterable[str], ...] = (
        diagnosis_result.priority_concept_ids,
        (
            concept_id
            for item_diagnosis in diagnosis_result.item_diagnoses
            for concept_id in item_diagnosis.prerequisite_gap_ids
        ),
        (
            target.concept_id
            for target in remediation_plan.ordered_targets()
            if target.is_high_priority()
        ),
        (
            state.concept_id
            for state in learner_state_snapshot.weak_concepts(
                weak_mastery_threshold
            )
        ),
    )

    selected = _stable_supported_targets(
        candidate_sources,
        diagnosed_concept_ids,
    )
    if not selected:
        _raise_reference_mismatch("no_diagnosis_supported_target")

    concept_state_ids = {
        state.concept_id for state in learner_state_snapshot.concept_states
    }
    if any(concept_id not in concept_state_ids for concept_id in selected):
        _raise_reference_mismatch("target_concept_state_missing")
    return selected


def _stable_supported_targets(
    candidate_sources: Iterable[Iterable[str]],
    diagnosed_concept_ids: set[str],
) -> list[str]:
    """Merge sources without reordering, duplication, or unsupported targets."""

    selected: list[str] = []
    seen: set[str] = set()
    for source in candidate_sources:
        for concept_id in source:
            if concept_id in diagnosed_concept_ids and concept_id not in seen:
                selected.append(concept_id)
                seen.add(concept_id)
    return selected


def _raise_reference_mismatch(reason: str) -> None:
    raise DomainError(
        code="TUTORING_REFERENCE_MISMATCH",
        module="m6",
        message="tutoring targets must align with diagnosis and learner state",
        details={"reason": reason},
    )
