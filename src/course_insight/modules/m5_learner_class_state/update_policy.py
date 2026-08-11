"""Transparent deterministic M5 state-update policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from course_insight.contracts.assessment import ScoreAuditRecord, ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.state import (
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    MisconceptionStrength,
)
from course_insight.infrastructure.json_io import read_json


@dataclass(frozen=True)
class StatePolicy:
    """Validated thresholds and class scope loaded from JSON."""

    aggregation_policy_version: str
    class_id: str
    class_size: int
    consolidating_threshold: float
    mastered_threshold: float
    minimum_assessed_count: int
    minimum_coverage: float
    misconception_activation_threshold: float

    @classmethod
    def from_path(cls, path: Path) -> "StatePolicy":
        """Load a strict policy document without silently applying defaults."""

        payload = read_json(path)
        try:
            policy = cls(**payload)
        except (TypeError, ValueError) as exc:
            raise DomainError(
                code="STATE_POLICY_INVALID",
                module="m5",
                message="state policy fields are missing or invalid",
                details={"path": str(path)},
            ) from exc
        probabilities = (
            policy.consolidating_threshold,
            policy.mastered_threshold,
            policy.minimum_coverage,
            policy.misconception_activation_threshold,
        )
        if (
            not policy.aggregation_policy_version
            or not policy.class_id
            or policy.class_size < 1
            or policy.minimum_assessed_count < 1
            or any(not 0.0 <= value <= 1.0 for value in probabilities)
            or policy.mastered_threshold <= policy.consolidating_threshold
        ):
            raise DomainError(
                code="STATE_POLICY_INVALID",
                module="m5",
                message="state policy thresholds or scope are inconsistent",
                details={"path": str(path)},
            )
        return policy


def audit_version_key(record: ScoreAuditRecord) -> str:
    """Return the replay-safe audit identity required by M5."""

    return f"{record.audit_id}:{record.audit_version}"


def latest_audits(bundle: ScoringResultBundle) -> list[ScoreAuditRecord]:
    """Return one isolated latest record per audit in stable identity order."""

    latest: dict[str, ScoreAuditRecord] = {}
    for record in bundle.score_audit_records:
        current = latest.get(record.audit_id)
        if current is None or record.audit_version > current.audit_version:
            latest = {**latest, record.audit_id: record}
    return [latest[audit_id].model_copy(deep=True) for audit_id in sorted(latest)]


class DeterministicStateUpdatePolicy:
    """Small explainable score-to-state rule for the default integration."""

    def build_diagnosis(
        self,
        bundle: ScoringResultBundle,
        knowledge: KnowledgeBundle,
    ) -> DiagnosisResult:
        """Diagnose latest audits using governed remediation references."""

        audits = latest_audits(bundle)
        if not audits:
            raise DomainError(
                code="INSUFFICIENT_EVIDENCE",
                module="m5",
                message="at least one score audit is required for state updating",
                recoverable=True,
            )
        target_concepts = _unique(
            [target.concept_id for target in bundle.remediation_plan.targets]
        ) or [knowledge.concepts[0].concept_id]
        target_misconceptions = _unique(
            [
                target.misconception_id
                for target in bundle.remediation_plan.targets
                if target.misconception_id is not None
            ]
        )
        concept_ids = {concept.concept_id for concept in knowledge.concepts}
        misconception_ids = {
            item.misconception_id for item in knowledge.misconception_tags
        }
        if not set(target_concepts) <= concept_ids or not set(
            target_misconceptions
        ) <= misconception_ids:
            raise DomainError(
                code="STATE_REFERENCE_MISMATCH",
                module="m5",
                message="remediation targets must exist in the knowledge bundle",
            )
        prerequisite_ids = _unique(
            [
                relation.from_concept_id
                for relation in knowledge.prerequisite_relations
                if relation.to_concept_id in target_concepts
            ]
        )
        diagnoses = [
            ItemDiagnosis(
                item_instance_id=audit.item_instance_id,
                concept_ids=target_concepts,
                misconception_ids=target_misconceptions,
                error_type=(
                    "misconception_or_low_confidence"
                    if audit.needs_review() or audit.total_score < audit.max_score
                    else "correct_with_review_evidence"
                ),
                confidence=audit.confidence,
                evidence_audit_ids=[audit_version_key(audit)],
                prerequisite_gap_ids=prerequisite_ids,
            )
            for audit in audits
        ]
        return DiagnosisResult(
            diagnosis_id=f"{bundle.attempt_id}_diagnosis_v{max(a.audit_version for a in audits)}",
            attempt_id=bundle.attempt_id,
            learner_id=bundle.learner_id,
            item_diagnoses=diagnoses,
            priority_concept_ids=target_concepts,
            priority_misconception_ids=target_misconceptions,
            generated_at=bundle.finalized_at,
        )

    def build_learner_state(
        self,
        bundle: ScoringResultBundle,
        knowledge: KnowledgeBundle,
        diagnosis: DiagnosisResult,
        previous: LearnerStateSnapshot | None,
        policy: StatePolicy,
    ) -> LearnerStateSnapshot:
        """Build a new immutable version from current latest audit evidence."""

        if previous is not None and (
            previous.course_id != knowledge.course_id
            or previous.class_id != policy.class_id
            or previous.learner_id != bundle.learner_id
        ):
            raise DomainError(
                code="STALE_STATE_VERSION",
                module="m5",
                message="previous learner state does not match this update scope",
                recoverable=True,
            )
        audits = latest_audits(bundle)
        score_ratio = bundle.total_score / bundle.max_score if bundle.max_score else 0.0
        review_required = bundle.requires_teacher_review()
        priority_concepts = set(diagnosis.priority_concept_ids)
        priority_misconceptions = set(diagnosis.priority_misconception_ids)
        # M5-03: build previous state lookups for cumulative merging
        prev_concepts = (
            {cs.concept_id: cs for cs in previous.concept_states}
            if previous is not None
            else {}
        )
        prev_misconceptions = (
            {
                ms.misconception_id: ms
                for cs in previous.concept_states
                for ms in cs.misconceptions
            }
            if previous is not None
            else {}
        )
        current_evidence = len(audits)
        concept_states: list[ConceptState] = []
        for concept in knowledge.concepts:
            prev_cs = prev_concepts.get(concept.concept_id)
            governed_misconceptions = [
                item
                for item in knowledge.misconception_tags
                if concept.concept_id in item.concept_ids
            ]
            misconception_states = [
                MisconceptionStrength(
                    misconception_id=item.misconception_id,
                    strength=(
                        0.6
                        if item.misconception_id in priority_misconceptions
                        and review_required
                        else 0.2
                    ),
                    evidence_count=(
                        (
                            prev_misconceptions[item.misconception_id].evidence_count
                            if item.misconception_id in prev_misconceptions
                            else 0
                        )
                        + (
                            current_evidence
                            if item.misconception_id in priority_misconceptions
                            else 0
                        )
                    ),
                    last_seen_at=bundle.finalized_at,
                )
                for item in governed_misconceptions
            ]
            is_priority = concept.concept_id in priority_concepts
            current_mastery = (
                score_ratio if is_priority else max(0.5, score_ratio)
            )
            # M5-03: weighted average merge with previous mastery
            if prev_cs is not None and prev_cs.evidence_count > 0:
                total_ev = prev_cs.evidence_count + current_evidence
                merged_mastery = (
                    prev_cs.mastery_probability * prev_cs.evidence_count
                    + current_mastery * current_evidence
                ) / total_ev
            else:
                merged_mastery = current_mastery
            concept_states.append(
                ConceptState(
                    concept_id=concept.concept_id,
                    mastery_probability=merged_mastery,
                    mastery_confidence=(0.6 if is_priority else 0.5),
                    misconceptions=misconception_states,
                    hint_dependency=(0.5 if is_priority and review_required else 0.0),
                    recent_correction_rate=(0.0 if review_required else score_ratio),
                    evidence_count=(
                        (prev_cs.evidence_count if prev_cs else 0)
                        + (current_evidence if is_priority else 0)
                    ),
                    updated_at=bundle.finalized_at,
                )
            )
        version = previous.next_version() if previous is not None else 1
        overall = sum(item.mastery_probability for item in concept_states) / len(
            concept_states
        )
        return LearnerStateSnapshot(
            snapshot_id=f"{bundle.learner_id}_state_v{version}",
            course_id=knowledge.course_id,
            class_id=policy.class_id,
            learner_id=bundle.learner_id,
            state_version=version,
            concept_states=concept_states,
            overall_mastery=overall,
            evidence_count=(
                (previous.evidence_count if previous is not None else 0)
                + current_evidence
            ),
            updated_at=bundle.finalized_at,
        )


def _unique(values: list[str]) -> list[str]:
    """Deduplicate references while preserving their governed order."""

    return list(dict.fromkeys(values))
