"""M9 analytics and teacher-review contracts owned by 冯."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from course_insight.contracts.assessment import ScoreAuditRecord
from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import (
    ClassConceptStatus,
    ClassMisconceptionSummary,
)


_SCORE_TOLERANCE = 1e-9


def _require_unique(values: list[str], *, entity: str) -> None:
    if len(values) != len(set(values)):
        raise DomainError(
            code="ANALYTICS_REFERENCE_CONFLICT",
            module="m9",
            message=f"{entity} identifiers must be unique",
        )


def _finite_sum(values: list[float], *, code: str, message: str) -> float:
    try:
        total = math.fsum(values)
    except (OverflowError, ValueError):
        total = None
    if total is None or not math.isfinite(total):
        raise DomainError(
            code=code,
            module="m9",
            message=message,
        ) from None
    return total


def _require_unit_threshold(value: float, *, name: str) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise DomainError(
            code="SUGGESTION_THRESHOLD_INVALID",
            module="m9",
            message="suggestion thresholds must be finite values from zero to one",
            details={name: value},
        )


class ClassReport(ContractModel):
    """Teacher-facing class report with explicit evidence coverage."""

    class_id: str = Field(min_length=1)
    coverage_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    concept_summaries: list[ClassConceptStatus]
    misconception_summaries: list[ClassMisconceptionSummary]
    score_statistics: dict[str, float]
    evidence_status: str = Field(min_length=1)

    @field_validator("score_statistics")
    @classmethod
    def _validate_score_statistics(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        if any(not key for key in value) or any(
            not math.isfinite(metric) for metric in value.values()
        ):
            raise ValueError("score statistics require named finite values")
        return dict(value)

    def validate_business_rules(self) -> None:
        """Require unique concept and misconception report entries."""

        _require_unique(
            [status.concept_id for status in self.concept_summaries],
            entity="class report concept",
        )
        _require_unique(
            [item.misconception_id for item in self.misconception_summaries],
            entity="class report misconception",
        )

    def is_actionable(self) -> bool:
        """Return whether sufficient covered evidence supports class action."""

        return (
            self.evidence_status == "sufficient"
            and self.coverage_rate > 0.0
            and bool(self.concept_summaries or self.misconception_summaries)
        )


class IndividualReport(ContractModel):
    """Teacher-facing learner summary for targeted follow-up."""

    learner_id: str = Field(min_length=1)
    overall_mastery: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    weak_concept_ids: list[str]
    active_misconception_ids: list[str]
    recent_score: float = Field(ge=0.0, allow_inf_nan=False)
    review_required_count: int = Field(ge=0)

    def validate_business_rules(self) -> None:
        """Require unambiguous concept and misconception references."""

        _require_unique(self.weak_concept_ids, entity="weak concept")
        _require_unique(
            self.active_misconception_ids,
            entity="active misconception",
        )

    def needs_follow_up(self) -> bool:
        """Return whether learning or review evidence calls for follow-up."""

        return bool(
            self.weak_concept_ids
            or self.active_misconception_ids
            or self.review_required_count
        )


class ReviewQueueItem(ContractModel):
    """One version-bound score audit waiting for teacher attention."""

    audit_id: str = Field(min_length=1)
    audit_version: int = Field(ge=1)
    learner_id: str = Field(min_length=1)
    item_instance_id: str = Field(min_length=1)
    recommended_score: float = Field(ge=0.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    review_reasons: list[str]

    def validate_business_rules(self) -> None:
        """Require distinct nonempty reasons for a queued review."""

        if any(not reason.strip() for reason in self.review_reasons):
            raise DomainError(
                code="REVIEW_REASON_INVALID",
                module="m9",
                message="review reasons must not be empty",
                details={"audit_id": self.audit_id},
            )
        _require_unique(self.review_reasons, entity="review reason")

    def priority_key(self) -> tuple[float, int]:
        """Return ascending confidence then descending reason-count priority."""

        return (self.confidence, -len(self.review_reasons))


class TeachingSuggestion(ContractModel):
    """Evidence-thresholded class teaching recommendation."""

    suggestion_id: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    concept_ids: list[str] = Field(min_length=1)
    content: str = Field(min_length=1)
    trigger_metrics: dict[str, float]
    affected_count: int = Field(ge=0)
    affected_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    coverage_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_ids: list[str]
    status: str = Field(min_length=1)

    @field_validator("trigger_metrics")
    @classmethod
    def _validate_trigger_metrics(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        if any(not key for key in value) or any(
            not math.isfinite(metric) for metric in value.values()
        ):
            raise ValueError("trigger metrics require named finite values")
        return dict(value)

    def validate_business_rules(self) -> None:
        """Require unique concept and evidence references."""

        _require_unique(self.concept_ids, entity="suggestion concept")
        _require_unique(self.evidence_ids, entity="suggestion evidence")

    def is_actionable(
        self,
        min_coverage: float,
        min_confidence: float,
    ) -> bool:
        """Apply caller-supplied evidence coverage and confidence cutoffs."""

        _require_unit_threshold(min_coverage, name="min_coverage")
        _require_unit_threshold(min_confidence, name="min_confidence")
        return (
            self.coverage_rate >= min_coverage
            and self.confidence >= min_confidence
            and self.affected_count > 0
            and bool(self.evidence_ids)
        )


class CriterionOverride(ContractModel):
    """Auditable before-and-after score for one rubric criterion."""

    criterion_id: str = Field(min_length=1)
    previous_score: float = Field(ge=0.0, allow_inf_nan=False)
    new_score: float = Field(ge=0.0, allow_inf_nan=False)
    reason: str = Field(min_length=1)

    def delta(self) -> float:
        """Return the finite score change introduced by this override."""

        difference = self.new_score - self.previous_score
        if not math.isfinite(difference):
            raise DomainError(
                code="OVERRIDE_TOTAL_MISMATCH",
                module="m9",
                message="criterion override delta must remain finite",
                details={"criterion_id": self.criterion_id},
            )
        return difference


class TeacherAnalyticsBundle(ContractModel):
    """Complete class, learner, review, and suggestion analytics payload."""

    report_id: str = Field(min_length=1)
    class_report: ClassReport
    individual_reports: list[IndividualReport]
    review_queue: list[ReviewQueueItem]
    teaching_suggestions: list[TeachingSuggestion]
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Require stable unique identities in every report collection."""

        _require_unique(
            [report.learner_id for report in self.individual_reports],
            entity="individual report learner",
        )
        queue_keys = [
            f"{item.audit_id}\x00{item.audit_version}" for item in self.review_queue
        ]
        _require_unique(queue_keys, entity="review queue audit version")
        _require_unique(
            [item.suggestion_id for item in self.teaching_suggestions],
            entity="teaching suggestion",
        )

    def open_review_count(self) -> int:
        """Return the number of version-specific audits awaiting review."""

        return len(self.review_queue)

    def actionable_suggestions(self) -> list[TeachingSuggestion]:
        """Return isolated suggestions that have positive evidence support."""

        return [
            suggestion.model_copy(deep=True)
            for suggestion in self.teaching_suggestions
            if suggestion.is_actionable(0.0, 0.0)
        ]

    def report_for_learner(self, learner_id: str) -> IndividualReport:
        """Return an isolated report for one pseudonymous learner."""

        for report in self.individual_reports:
            if report.learner_id == learner_id:
                return report.model_copy(deep=True)
        raise DomainError(
            code="INDIVIDUAL_REPORT_NOT_FOUND",
            module="m9",
            message="the requested learner report was not found",
            details={"learner_id": learner_id},
        )


class TeacherReviewDecision(ContractModel):
    """Optimistically versioned teacher decision for one score audit."""

    decision_id: str = Field(min_length=1)
    audit_id: str = Field(min_length=1)
    expected_audit_version: int = Field(ge=1)
    decision: Literal["confirm", "override", "reject"]
    final_total_score: float = Field(ge=0.0, allow_inf_nan=False)
    criterion_overrides: list[CriterionOverride]
    teacher_comment: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    reviewed_at: datetime

    def validate_business_rules(self) -> None:
        """Require complete, unique overrides and a conserved override total."""

        _require_unique(
            [override.criterion_id for override in self.criterion_overrides],
            entity="criterion override",
        )
        if self.decision != "override" and self.criterion_overrides:
            raise DomainError(
                code="REVIEW_DECISION_INVALID",
                module="m9",
                message="only override decisions may carry criterion overrides",
                details={"decision_id": self.decision_id},
            )
        if self.decision == "override":
            if not self.criterion_overrides or not math.isclose(
                self.override_score_sum(),
                self.final_total_score,
                rel_tol=0.0,
                abs_tol=_SCORE_TOLERANCE,
            ):
                raise DomainError(
                    code="OVERRIDE_TOTAL_MISMATCH",
                    module="m9",
                    message="override criterion scores must equal the final total",
                    details={"decision_id": self.decision_id},
                )

    def is_override(self) -> bool:
        """Return whether the teacher supplied criterion score replacements."""

        return self.decision == "override"

    def override_score_sum(self) -> float:
        """Return a stable finite sum of replacement criterion scores."""

        return _finite_sum(
            [override.new_score for override in self.criterion_overrides],
            code="OVERRIDE_TOTAL_MISMATCH",
            message="override criterion scores must have a finite total",
        )

    def assert_matches(self, record: ScoreAuditRecord) -> None:
        """Require current audit identity, version, scores, and criterion links."""

        if self.audit_id != record.audit_id:
            raise DomainError(
                code="REVIEW_REFERENCE_MISMATCH",
                module="m9",
                message="teacher decision must reference the requested score audit",
                details={
                    "decision_audit_id": self.audit_id,
                    "record_audit_id": record.audit_id,
                },
            )
        if self.expected_audit_version != record.audit_version:
            raise DomainError(
                code="REVIEW_VERSION_CONFLICT",
                module="m9",
                message="score audit changed before the teacher decision was applied",
                details={
                    "expected_audit_version": self.expected_audit_version,
                    "record_audit_version": record.audit_version,
                },
                recoverable=True,
            )
        if self.final_total_score > record.max_score:
            raise DomainError(
                code="REVIEW_SCORE_INVALID",
                module="m9",
                message="teacher decision total cannot exceed the audit maximum",
                details={"audit_id": record.audit_id},
            )
        if not self.is_override():
            if self.decision == "confirm" and not math.isclose(
                self.final_total_score,
                record.total_score,
                rel_tol=0.0,
                abs_tol=_SCORE_TOLERANCE,
            ):
                raise DomainError(
                    code="REVIEW_TOTAL_MISMATCH",
                    module="m9",
                    message="confirmed total must retain the audited score",
                    details={"audit_id": record.audit_id},
                )
            return

        record_scores = {
            criterion.criterion_id: criterion.score
            for criterion in record.criterion_scores
        }
        override_scores = {
            override.criterion_id: override.previous_score
            for override in self.criterion_overrides
        }
        if record_scores.keys() != override_scores.keys() or any(
            not math.isclose(
                previous_score,
                record_scores[criterion_id],
                rel_tol=0.0,
                abs_tol=_SCORE_TOLERANCE,
            )
            for criterion_id, previous_score in override_scores.items()
        ):
            raise DomainError(
                code="OVERRIDE_REFERENCE_MISMATCH",
                module="m9",
                message="criterion overrides must cover and match the current audit",
                details={"audit_id": record.audit_id},
            )
