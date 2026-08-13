"""M5 learner and class state contracts owned by 童."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


_PROBABILITY_TOLERANCE = 1e-9


def _require_unique(
    values: list[str],
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    if len(values) != len(set(values)):
        raise DomainError(
            code=code,
            module="m5",
            message=message,
            details=details,
        )


def _require_unit_threshold(value: float, *, name: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise DomainError(
            code="MASTERY_THRESHOLD_INVALID",
            module="m5",
            message="mastery thresholds must be finite values from zero to one",
            details={name: value},
        )


class ItemDiagnosis(ContractModel):
    """Diagnosis evidence and misconceptions for one attempted item."""

    item_instance_id: str = Field(min_length=1)
    concept_ids: list[str] = Field(min_length=1)
    misconception_ids: list[str]
    error_type: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_audit_ids: list[str] = Field(min_length=1)
    prerequisite_gap_ids: list[str]

    def validate_business_rules(self) -> None:
        """Require unambiguous concept, misconception, and audit links."""

        references = (
            (self.concept_ids, "concept"),
            (self.misconception_ids, "misconception"),
            (self.evidence_audit_ids, "audit"),
            (self.prerequisite_gap_ids, "prerequisite gap"),
        )
        for values, reference_type in references:
            _require_unique(
                values,
                code="DIAGNOSIS_REFERENCE_CONFLICT",
                message=f"item diagnosis {reference_type} references must be unique",
                details={"item_instance_id": self.item_instance_id},
            )

    def has_misconception(self) -> bool:
        """Return whether the item diagnosis identifies a misconception."""

        return bool(self.misconception_ids)


class DiagnosisResult(ContractModel):
    """Attempt-wide diagnosis with ordered teaching priorities."""

    diagnosis_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    item_diagnoses: list[ItemDiagnosis] = Field(min_length=1)
    priority_concept_ids: list[str]
    priority_misconception_ids: list[str]
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Keep item identities and priority targets internally consistent."""

        _require_unique(
            [item.item_instance_id for item in self.item_diagnoses],
            code="DIAGNOSIS_REFERENCE_CONFLICT",
            message="diagnosed item identifiers must be unique",
            details={"diagnosis_id": self.diagnosis_id},
        )
        _require_unique(
            self.priority_concept_ids,
            code="DIAGNOSIS_REFERENCE_CONFLICT",
            message="priority concept identifiers must be unique",
            details={"diagnosis_id": self.diagnosis_id},
        )
        _require_unique(
            self.priority_misconception_ids,
            code="DIAGNOSIS_REFERENCE_CONFLICT",
            message="priority misconception identifiers must be unique",
            details={"diagnosis_id": self.diagnosis_id},
        )
        diagnosed_concepts = {
            concept_id
            for item in self.item_diagnoses
            for concept_id in item.concept_ids
        }
        diagnosed_misconceptions = {
            misconception_id
            for item in self.item_diagnoses
            for misconception_id in item.misconception_ids
        }
        if not set(self.priority_concept_ids) <= diagnosed_concepts or not set(
            self.priority_misconception_ids
        ) <= diagnosed_misconceptions:
            raise DomainError(
                code="DIAGNOSIS_REFERENCE_MISMATCH",
                module="m5",
                message="priority targets must be present in the item diagnoses",
                details={"diagnosis_id": self.diagnosis_id},
            )

    def diagnosis_for_item(self, item_instance_id: str) -> ItemDiagnosis:
        """Return an isolated diagnosis for one paper item."""

        for diagnosis in self.item_diagnoses:
            if diagnosis.item_instance_id == item_instance_id:
                return diagnosis.model_copy(deep=True)
        raise DomainError(
            code="ITEM_DIAGNOSIS_NOT_FOUND",
            module="m5",
            message="the requested item diagnosis was not found",
            details={"item_instance_id": item_instance_id},
        )

    def has_prerequisite_gap(self) -> bool:
        """Return whether any diagnosed item has a prerequisite gap."""

        return any(item.prerequisite_gap_ids for item in self.item_diagnoses)

    def top_targets(self, limit: int) -> list[str]:
        """Return ordered concept targets followed by misconception targets."""

        if limit < 0:
            raise DomainError(
                code="INVALID_LIMIT",
                module="m5",
                message="target limit must not be negative",
                details={"limit": limit},
            )
        return [*self.priority_concept_ids, *self.priority_misconception_ids][
            :limit
        ]


class MisconceptionStrength(ContractModel):
    """Current evidence-weighted strength of one misconception."""

    misconception_id: str = Field(min_length=1)
    strength: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_count: int = Field(ge=0)
    last_seen_at: datetime

    def is_active(self, threshold: float) -> bool:
        """Return whether strength meets a validated activation threshold."""

        _require_unit_threshold(threshold, name="threshold")
        return self.strength >= threshold


class ConceptState(ContractModel):
    """Learner state for one course concept."""

    concept_id: str = Field(min_length=1)
    mastery_probability: float = Field(
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    mastery_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    misconceptions: list[MisconceptionStrength]
    hint_dependency: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    recent_correction_rate: float = Field(
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    evidence_count: int = Field(ge=0)
    updated_at: datetime

    def validate_business_rules(self) -> None:
        """Require one state entry for each misconception identifier."""

        _require_unique(
            [item.misconception_id for item in self.misconceptions],
            code="CONCEPT_STATE_REFERENCE_CONFLICT",
            message="concept-state misconception identifiers must be unique",
            details={"concept_id": self.concept_id},
        )

    def band(self, mastered: float, consolidating: float) -> str:
        """Classify mastery using ordered mastered and consolidating cutoffs."""

        _require_unit_threshold(mastered, name="mastered")
        _require_unit_threshold(consolidating, name="consolidating")
        if mastered <= consolidating:
            raise DomainError(
                code="MASTERY_THRESHOLD_INVALID",
                module="m5",
                message="mastered threshold must exceed consolidating threshold",
                details={
                    "mastered": mastered,
                    "consolidating": consolidating,
                },
            )
        if self.mastery_probability >= mastered:
            return "mastered"
        if self.mastery_probability >= consolidating:
            return "consolidating"
        return "priority_support"

    def active_misconceptions(
        self,
        threshold: float,
    ) -> list[MisconceptionStrength]:
        """Return isolated active misconceptions in stored order."""

        _require_unit_threshold(threshold, name="threshold")
        return [
            item.model_copy(deep=True)
            for item in self.misconceptions
            if item.strength >= threshold
        ]


class LearnerStateSnapshot(ContractModel):
    """Versioned learner-state snapshot consumed across later modules."""

    snapshot_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    state_version: int = Field(ge=1)
    concept_states: list[ConceptState]
    overall_mastery: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_count: int = Field(ge=0)
    model_run_id: str | None = Field(default=None, min_length=1)
    dina_model_version: str | None = Field(default=None, min_length=1)
    bkt_model_version: str | None = Field(default=None, min_length=1)
    observation_watermark: str | None = Field(default=None, min_length=1)
    updated_at: datetime

    def validate_business_rules(self) -> None:
        """Require one state entry for each concept identifier."""

        _require_unique(
            [state.concept_id for state in self.concept_states],
            code="LEARNER_STATE_REFERENCE_CONFLICT",
            message="learner concept-state identifiers must be unique",
            details={"snapshot_id": self.snapshot_id},
        )
        evidence_fields = (
            self.dina_model_version,
            self.bkt_model_version,
            self.observation_watermark,
        )
        if self.model_run_id is None and any(
            value is not None for value in evidence_fields
        ):
            raise DomainError(
                code="LEARNER_MODEL_EVIDENCE_INVALID",
                module="m5",
                message="learner model evidence requires a model run identity",
            )
        if self.model_run_id is not None and any(
            value is None for value in evidence_fields
        ):
            raise DomainError(
                code="LEARNER_MODEL_EVIDENCE_INVALID",
                module="m5",
                message="model-backed learner state requires complete model evidence",
            )

    def get_concept_state(self, concept_id: str) -> ConceptState:
        """Return an isolated state for the requested concept."""

        for state in self.concept_states:
            if state.concept_id == concept_id:
                return state.model_copy(deep=True)
        raise DomainError(
            code="CONCEPT_STATE_NOT_FOUND",
            module="m5",
            message="the requested learner concept state was not found",
            details={"concept_id": concept_id},
        )

    def weak_concepts(self, threshold: float) -> list[ConceptState]:
        """Return isolated states whose mastery is below the cutoff."""

        _require_unit_threshold(threshold, name="threshold")
        return [
            state.model_copy(deep=True)
            for state in self.concept_states
            if state.mastery_probability < threshold
        ]

    def next_version(self) -> int:
        """Return the monotonic version expected for the next update."""

        return self.state_version + 1


class MasteryDistribution(ContractModel):
    """Normalized class proportions across the three mastery bands."""

    mastered: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    consolidating: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    priority_support: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    def validate_business_rules(self) -> None:
        """Require the three class proportions to sum to one."""

        total = self.sum()
        if not math.isclose(
            total,
            1.0,
            rel_tol=0.0,
            abs_tol=_PROBABILITY_TOLERANCE,
        ):
            raise DomainError(
                code="MASTERY_DISTRIBUTION_INVALID",
                module="m5",
                message="mastery distribution proportions must sum to one",
                details={"sum": total},
            )

    def sum(self) -> float:
        """Return a stable finite total for the mastery proportions."""

        try:
            total = math.fsum(
                [self.mastered, self.consolidating, self.priority_support]
            )
        except (OverflowError, ValueError):
            total = None
        if total is None or not math.isfinite(total):
            raise DomainError(
                code="MASTERY_DISTRIBUTION_INVALID",
                module="m5",
                message="mastery distribution must have a finite sum",
            ) from None
        return total


class ClassConceptStatus(ContractModel):
    """Aggregated class evidence for one concept."""

    concept_id: str = Field(min_length=1)
    mastery_distribution: MasteryDistribution
    mean_mastery_probability: float = Field(
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    mean_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    mastery_trend_delta: float | None = Field(allow_inf_nan=False)
    trend_comparable: bool
    sample_count: int = Field(ge=0)

    def validate_business_rules(self) -> None:
        """Keep trend comparability and its optional delta synchronized."""

        if self.trend_comparable != (self.mastery_trend_delta is not None):
            raise DomainError(
                code="TREND_REFERENCE_MISMATCH",
                module="m5",
                message="a trend delta is required exactly when snapshots are comparable",
                details={"concept_id": self.concept_id},
            )

    def needs_support(self, threshold: float) -> bool:
        """Return whether the priority-support share meets the cutoff."""

        _require_unit_threshold(threshold, name="threshold")
        return self.mastery_distribution.priority_support >= threshold


class ClassMisconceptionSummary(ContractModel):
    """Aggregated prevalence of one misconception in assessed learners."""

    misconception_id: str = Field(min_length=1)
    affected_count: int = Field(ge=0)
    affected_rate_among_assessed: float = Field(
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    evidence_attempts: int = Field(ge=0)

    def validate_business_rules(self) -> None:
        """Ensure affected learners do not exceed supporting attempts."""

        if self.affected_count > self.evidence_attempts:
            raise DomainError(
                code="MISCONCEPTION_EVIDENCE_MISMATCH",
                module="m5",
                message="affected count cannot exceed misconception evidence attempts",
                details={"misconception_id": self.misconception_id},
            )

    def is_frequent(self, threshold: float) -> bool:
        """Return whether assessed prevalence meets the cutoff."""

        _require_unit_threshold(threshold, name="threshold")
        return self.affected_rate_among_assessed >= threshold


class ClassStateSnapshot(ContractModel):
    """Versioned class aggregate with explicit evidence sufficiency."""

    snapshot_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    aggregation_policy_version: str = Field(min_length=1)
    scope: dict[str, str]
    class_size: int = Field(ge=0)
    assessed_count: int = Field(ge=0)
    coverage_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    concept_status: list[ClassConceptStatus]
    misconception_summary: list[ClassMisconceptionSummary]
    evidence_status: Literal["sufficient", "insufficient"]
    model_run_ids: list[str] = Field(default_factory=list)
    updated_at: datetime

    def validate_business_rules(self) -> None:
        """Validate aggregate counts, coverage, and stable nested identities."""

        if self.assessed_count > self.class_size:
            raise DomainError(
                code="CLASS_COVERAGE_MISMATCH",
                module="m5",
                message="assessed learner count cannot exceed class size",
                details={
                    "class_size": self.class_size,
                    "assessed_count": self.assessed_count,
                },
            )
        expected_coverage = (
            self.assessed_count / self.class_size if self.class_size else 0.0
        )
        if not math.isclose(
            self.coverage_rate,
            expected_coverage,
            rel_tol=0.0,
            abs_tol=_PROBABILITY_TOLERANCE,
        ):
            raise DomainError(
                code="CLASS_COVERAGE_MISMATCH",
                module="m5",
                message="coverage rate must equal assessed count divided by class size",
                details={
                    "coverage_rate": self.coverage_rate,
                    "expected_coverage": expected_coverage,
                },
            )
        _require_unique(
            [status.concept_id for status in self.concept_status],
            code="CLASS_STATE_REFERENCE_CONFLICT",
            message="class concept identifiers must be unique",
            details={"snapshot_id": self.snapshot_id},
        )
        _require_unique(
            self.model_run_ids,
            code="CLASS_MODEL_EVIDENCE_CONFLICT",
            message="class model run identities must be unique",
            details={"snapshot_id": self.snapshot_id},
        )
        _require_unique(
            [item.misconception_id for item in self.misconception_summary],
            code="CLASS_STATE_REFERENCE_CONFLICT",
            message="class misconception identifiers must be unique",
            details={"snapshot_id": self.snapshot_id},
        )
        if any(status.sample_count > self.assessed_count for status in self.concept_status):
            raise DomainError(
                code="CLASS_SAMPLE_COUNT_MISMATCH",
                module="m5",
                message="concept sample count cannot exceed assessed learner count",
                details={"snapshot_id": self.snapshot_id},
            )
        if any(
            item.affected_count > self.assessed_count
            for item in self.misconception_summary
        ):
            raise DomainError(
                code="CLASS_SAMPLE_COUNT_MISMATCH",
                module="m5",
                message="misconception affected count cannot exceed assessed learners",
                details={"snapshot_id": self.snapshot_id},
            )

    def get_concept_status(self, concept_id: str) -> ClassConceptStatus:
        """Return an isolated aggregate for the requested concept."""

        for status in self.concept_status:
            if status.concept_id == concept_id:
                return status.model_copy(deep=True)
        raise DomainError(
            code="CLASS_CONCEPT_STATUS_NOT_FOUND",
            module="m5",
            message="the requested class concept status was not found",
            details={"concept_id": concept_id},
        )

    def is_actionable(self, min_coverage: float, min_count: int) -> bool:
        """Return whether evidence sufficiency and coverage permit action."""

        _require_unit_threshold(min_coverage, name="min_coverage")
        if min_count < 0:
            raise DomainError(
                code="INVALID_MINIMUM_COUNT",
                module="m5",
                message="minimum assessed count must not be negative",
                details={"min_count": min_count},
            )
        return (
            self.evidence_status == "sufficient"
            and self.coverage_rate >= min_coverage
            and self.assessed_count >= min_count
        )

    def top_misconceptions(
        self,
        limit: int,
    ) -> list[ClassMisconceptionSummary]:
        """Return isolated misconception summaries ranked by prevalence."""

        if limit < 0:
            raise DomainError(
                code="INVALID_LIMIT",
                module="m5",
                message="ranking limit must not be negative",
                details={"limit": limit},
            )
        ordered = sorted(
            self.misconception_summary,
            key=lambda item: (
                item.affected_rate_among_assessed,
                item.affected_count,
                item.evidence_attempts,
                item.misconception_id,
            ),
            reverse=True,
        )
        return [item.model_copy(deep=True) for item in ordered[:limit]]


class StateUpdateResult(ContractModel):
    """Atomic diagnosis plus learner and class state update result."""

    diagnosis_result: DiagnosisResult
    learner_state_snapshot: LearnerStateSnapshot
    class_state_snapshot: ClassStateSnapshot
    processed_audit_ids: list[str]
    updated_at: datetime

    def validate_business_rules(self) -> None:
        """Apply cross-object identity and audit-coverage validation."""

        self.assert_consistent()

    def assert_consistent(self) -> None:
        """Require aligned learner, course, class, and audit references."""

        learner = self.learner_state_snapshot
        class_state = self.class_state_snapshot
        if (
            self.diagnosis_result.learner_id != learner.learner_id
            or learner.course_id != class_state.course_id
            or learner.class_id != class_state.class_id
        ):
            raise DomainError(
                code="STATE_REFERENCE_MISMATCH",
                module="m5",
                message="diagnosis, learner state, and class state identities must align",
            )
        _require_unique(
            self.processed_audit_ids,
            code="STATE_AUDIT_REFERENCE_CONFLICT",
            message="processed audit identifiers must be unique",
        )
        evidence_audit_ids = {
            audit_id
            for item in self.diagnosis_result.item_diagnoses
            for audit_id in item.evidence_audit_ids
        }
        if not evidence_audit_ids <= set(self.processed_audit_ids):
            raise DomainError(
                code="STATE_AUDIT_REFERENCE_MISMATCH",
                module="m5",
                message="processed audits must cover all diagnosis evidence",
                details={
                    "missing_audit_ids": sorted(
                        evidence_audit_ids - set(self.processed_audit_ids)
                    )
                },
            )

    def contains_audit(self, audit_id: str) -> bool:
        """Return whether the update processed the requested audit."""

        return audit_id in self.processed_audit_ids
