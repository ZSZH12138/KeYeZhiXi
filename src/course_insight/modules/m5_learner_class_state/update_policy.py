"""Transparent deterministic M5 state-update policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from course_insight.contracts.assessment import ScoreAuditRecord, ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import LearningObservationBatch
from course_insight.contracts.state import (
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    MisconceptionStrength,
)


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

        try:
            content = path.read_bytes()
        except (OSError, TypeError, ValueError) as exc:
            raise DomainError(
                code="STATE_POLICY_INVALID",
                module="m5",
                message="state policy could not be loaded",
                details={
                    "policy": "state",
                    "reason": type(exc).__name__,
                },
                recoverable=True,
            ) from exc
        return cls.from_bytes(content)

    @classmethod
    def from_bytes(cls, content: bytes) -> "StatePolicy":
        """Parse one already-read strict policy document."""

        try:
            payload = json.loads(
                content.decode("utf-8"),
                parse_constant=_reject_nonfinite_json_constant,
            )
            policy = cls(**payload)
        except (TypeError, UnicodeError, ValueError) as exc:
            raise DomainError(
                code="STATE_POLICY_INVALID",
                module="m5",
                message="state policy fields are missing or invalid",
                details={
                    "policy": "state",
                    "reason": type(exc).__name__,
                },
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
                details={"policy": "state"},
            )
        return policy


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


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
        observations: LearningObservationBatch | None = None,
    ) -> DiagnosisResult:
        """Diagnose each audit through its authoritative frozen item mapping."""

        audits = latest_audits(bundle)
        if not audits:
            raise DomainError(
                code="INSUFFICIENT_EVIDENCE",
                module="m5",
                message="at least one score audit is required for state updating",
                recoverable=True,
            )
        if observations is not None:
            return self._build_authoritative_diagnosis(
                bundle,
                knowledge,
                observations,
                audits,
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

    @staticmethod
    def _build_authoritative_diagnosis(
        bundle: ScoringResultBundle,
        knowledge: KnowledgeBundle,
        observations: LearningObservationBatch,
        audits: list[ScoreAuditRecord],
    ) -> DiagnosisResult:
        if observations.learner_id != bundle.learner_id:
            raise DomainError(
                code="STATE_REFERENCE_MISMATCH",
                module="m5",
                message="observation batch learner does not match scoring evidence",
            )
        observation_by_audit = {
            (item.source_audit_id, item.source_audit_version): item
            for item in observations.observations
            if item.attempt_id == bundle.attempt_id
        }
        audit_keys = {(item.audit_id, item.audit_version) for item in audits}
        if set(observation_by_audit) != audit_keys:
            raise DomainError(
                code="STATE_REFERENCE_MISMATCH",
                module="m5",
                message="observations must exactly cover current score audits",
                details={"attempt_id": bundle.attempt_id},
            )

        q_concepts: dict[tuple[str, str], list[str]] = {}
        for entry in knowledge.q_matrix:
            if not entry.is_active():
                continue
            key = (entry.item_id, entry.item_version)
            q_concepts = {
                **q_concepts,
                key: [*q_concepts.get(key, []), entry.concept_id],
            }
        diagnosis_rows: list[ItemDiagnosis] = []
        for audit in audits:
            observation = observation_by_audit[(audit.audit_id, audit.audit_version)]
            item_key = (observation.item_id, observation.item_version)
            concepts = _unique(q_concepts.get(item_key, []))
            if not concepts or set(concepts) != set(observation.concept_ids):
                raise DomainError(
                    code="STATE_REFERENCE_MISMATCH",
                    module="m5",
                    message="frozen item concepts do not match the governed Q-matrix",
                    details={
                        "item_id": observation.item_id,
                        "item_version": observation.item_version,
                    },
                )
            if (
                observation.score != audit.total_score
                or observation.max_score != audit.max_score
            ):
                raise DomainError(
                    code="STATE_REFERENCE_MISMATCH",
                    module="m5",
                    message="observation score differs from its source audit",
                    details={"audit_id": audit.audit_id},
                )
            item = knowledge.get_item(*item_key)
            misconceptions = (
                list(item.misconception_ids)
                if observation.response_outcome == "incorrect"
                else []
            )
            prerequisites = _unique(
                [
                    relation.from_concept_id
                    for relation in knowledge.prerequisite_relations
                    if relation.to_concept_id in concepts
                ]
            )
            diagnosis_rows.append(
                ItemDiagnosis(
                    item_instance_id=audit.item_instance_id,
                    concept_ids=concepts,
                    misconception_ids=misconceptions,
                    error_type=(
                        "correct"
                        if observation.response_outcome == "correct"
                        else "incorrect"
                    ),
                    confidence=audit.confidence,
                    evidence_audit_ids=[audit_version_key(audit)],
                    prerequisite_gap_ids=prerequisites,
                )
            )

        diagnosed_concepts = _unique(
            [concept for row in diagnosis_rows for concept in row.concept_ids]
        )
        diagnosed_misconceptions = _unique(
            [
                misconception
                for row in diagnosis_rows
                for misconception in row.misconception_ids
            ]
        )
        requested_concepts = _unique(
            [target.concept_id for target in bundle.remediation_plan.targets]
        )
        requested_misconceptions = _unique(
            [
                target.misconception_id
                for target in bundle.remediation_plan.targets
                if target.misconception_id is not None
            ]
        )
        priority_concepts = requested_concepts or diagnosed_concepts
        priority_misconceptions = (
            requested_misconceptions or diagnosed_misconceptions
        )
        if not set(priority_concepts) <= set(diagnosed_concepts) or not set(
            priority_misconceptions
        ) <= set(diagnosed_misconceptions):
            raise DomainError(
                code="STATE_REFERENCE_MISMATCH",
                module="m5",
                message="remediation priorities are absent from item diagnoses",
            )
        return DiagnosisResult(
            diagnosis_id=(
                f"{bundle.attempt_id}_diagnosis_v"
                f"{max(item.audit_version for item in audits)}"
            ),
            attempt_id=bundle.attempt_id,
            learner_id=bundle.learner_id,
            item_diagnoses=diagnosis_rows,
            priority_concept_ids=priority_concepts,
            priority_misconception_ids=priority_misconceptions,
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
        concept_states: list[ConceptState] = []
        for concept in knowledge.concepts:
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
                        len(audits)
                        if item.misconception_id in priority_misconceptions
                        else 0
                    ),
                    last_seen_at=bundle.finalized_at,
                )
                for item in governed_misconceptions
            ]
            is_priority = concept.concept_id in priority_concepts
            concept_states.append(
                ConceptState(
                    concept_id=concept.concept_id,
                    mastery_probability=(score_ratio if is_priority else max(0.5, score_ratio)),
                    mastery_confidence=(0.6 if is_priority else 0.5),
                    misconceptions=misconception_states,
                    hint_dependency=(0.5 if is_priority and review_required else 0.0),
                    recent_correction_rate=(0.0 if review_required else score_ratio),
                    evidence_count=(len(audits) if is_priority else 0),
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
            evidence_count=len(audits),
            updated_at=bundle.finalized_at,
        )


def _unique(values: list[str]) -> list[str]:
    """Deduplicate references while preserving their governed order."""

    return list(dict.fromkeys(values))
