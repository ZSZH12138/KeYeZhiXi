"""M7/M8 assessment contracts shared by 童 and 冯."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from course_insight.contracts._assessment_support import (
    COMPLETED_REVIEW_STATUSES as _COMPLETED_REVIEW_STATUSES,
    REJECTED_REVIEW_STATUSES as _REJECTED_REVIEW_STATUSES,
    SCORE_TOLERANCE as _SCORE_TOLERANCE,
    copy_json_value as _copy_json_value,
    ensure_lossless_json as _ensure_lossless_json,
    finite_score_sum as _finite_score_sum,
    latest_audit_records as _latest_audit_records,
    require_unique as _require_unique,
    validate_audit_history as _validate_audit_history,
    validate_learning_event_references as _validate_learning_event_references,
)
from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.knowledge import Rubric


class ItemInstance(ContractModel):
    """Frozen paper item with resolved parameters, score, and evidence links."""

    item_instance_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    item_version: str = Field(min_length=1)
    stem: str = Field(min_length=1)
    parameters: dict[str, Any]
    concept_ids: list[str] = Field(min_length=1)
    rubric_id: str | None = Field(min_length=1)
    max_score: float = Field(ge=0.0, allow_inf_nan=False)
    source_evidence_ids: list[str]

    @field_validator("parameters", mode="before")
    @classmethod
    def _validate_and_copy_parameters(cls, value: Any) -> dict[str, Any]:
        copied = _copy_json_value(value, active_container_ids=set())
        if type(copied) is not dict:
            raise ValueError("parameters must be a JSON object")
        _ensure_lossless_json(copied)
        return copied

    def validate_business_rules(self) -> None:
        """Require unambiguous concept and source-evidence references."""

        _require_unique(
            self.concept_ids,
            code="DUPLICATE_ITEM_REFERENCE",
            message="item instance concept references must be unique",
            details={"item_instance_id": self.item_instance_id},
        )
        _require_unique(
            self.source_evidence_ids,
            code="DUPLICATE_ITEM_REFERENCE",
            message="item instance source-evidence references must be unique",
            details={"item_instance_id": self.item_instance_id},
        )

    def instance_checksum(self) -> str:
        """Return a canonical identity for all frozen item-instance fields."""

        return self.content_checksum()

    def is_subjective(self) -> bool:
        """Return whether this item is bound to a scoring rubric."""

        return self.rubric_id is not None


class PaperSection(ContractModel):
    """One paper section whose declared score equals its item maxima."""

    section_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    items: list[ItemInstance]
    score: float = Field(ge=0.0, allow_inf_nan=False)

    def validate_business_rules(self) -> None:
        """Validate unique item instances and section score conservation."""

        _require_unique(
            [item.item_instance_id for item in self.items],
            code="PAPER_ITEM_REFERENCE_CONFLICT",
            message="item instance identifiers must be unique within a section",
            details={"section_id": self.section_id},
        )
        item_total = self.score_sum()
        if not math.isclose(
            item_total,
            self.score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            raise DomainError(
                code="PAPER_SECTION_SCORE_MISMATCH",
                module="m8",
                message="paper section score must equal its item maxima",
                details={
                    "section_id": self.section_id,
                    "item_total": item_total,
                    "score": self.score,
                },
            )

    def score_sum(self) -> float:
        """Return the numerically stable sum of frozen item maxima."""

        return _finite_score_sum(
            [item.max_score for item in self.items],
            code="PAPER_SECTION_SCORE_MISMATCH",
            message="paper section item maxima must have a finite sum",
            details={"section_id": self.section_id},
        )


class AssessmentPaper(ContractModel):
    """Version-frozen M8 paper produced from one TaskPlan blueprint."""

    paper_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    blueprint_id: str = Field(min_length=1)
    blueprint_version: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    sections: list[PaperSection] = Field(min_length=1)
    generated_at: datetime
    immutable_checksum: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Require unique sections and globally unique item instances."""

        _require_unique(
            [section.section_id for section in self.sections],
            code="PAPER_SECTION_REFERENCE_CONFLICT",
            message="paper section identifiers must be unique",
            details={"paper_id": self.paper_id},
        )
        item_ids = [
            item.item_instance_id
            for section in self.sections
            for item in section.items
        ]
        _require_unique(
            item_ids,
            code="PAPER_ITEM_REFERENCE_CONFLICT",
            message="item instance identifiers must be unique across a paper",
            details={"paper_id": self.paper_id},
        )

    def all_items(self) -> list[ItemInstance]:
        """Return flattened item instances as independent deep copies."""

        return [
            item.model_copy(deep=True)
            for section in self.sections
            for item in section.items
        ]

    def get_item_instance(self, item_instance_id: str) -> ItemInstance:
        """Return the requested frozen item without exposing paper state."""

        for section in self.sections:
            for item in section.items:
                if item.item_instance_id == item_instance_id:
                    return item.model_copy(deep=True)
        raise DomainError(
            code="ITEM_INSTANCE_NOT_FOUND",
            module="m8",
            message="paper item instance was not found",
            details={"item_instance_id": item_instance_id},
        )

    def total_score(self) -> float:
        """Return the finite total of declared paper-section scores."""

        return _finite_score_sum(
            [section.score for section in self.sections],
            code="PAPER_TOTAL_INVALID",
            message="paper section scores must have a finite total",
            details={"paper_id": self.paper_id},
        )

    def freeze(self) -> str:
        """Return a deterministic checksum that excludes its stored checksum."""

        return self.content_checksum()


class CriterionScore(ContractModel):
    """One auditable rubric-criterion decision and its evidence."""

    criterion_id: str = Field(min_length=1)
    score: float = Field(ge=0.0, allow_inf_nan=False)
    student_evidence: str
    course_evidence_id: str | None = Field(min_length=1)
    reason: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Require an answer quotation whenever the criterion earns credit."""

        if self.score > 0.0 and not self.has_student_evidence():
            raise DomainError(
                code="STUDENT_EVIDENCE_REQUIRED",
                module="m7",
                message="a positive criterion score requires student evidence",
                details={"criterion_id": self.criterion_id},
            )

    def has_student_evidence(self) -> bool:
        """Return whether the stored student quotation contains visible text."""

        return bool(self.student_evidence.strip())


class RubricScoringTask(ContractModel):
    """M8 constructed-response task passed unchanged to governed DeepSeek scoring."""

    scoring_task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    item_instance: ItemInstance
    student_answer: str
    question_type: Literal["fill_blank", "subjective"] = "subjective"
    reference_answers: list[str] = Field(default_factory=list)
    concept_names: list[str] = Field(default_factory=list)
    review_confidence_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    rubric: Rubric
    evidence_query_id: str = Field(min_length=1)
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Bind the exact frozen item score to the exact rubric version."""

        _require_unique(
            self.reference_answers,
            code="DUPLICATE_REFERENCE_ANSWER",
            message="reference answers must be unique within a scoring task",
            details={"scoring_task_id": self.scoring_task_id},
        )
        maximum_matches = math.isclose(
            self.item_instance.max_score,
            self.rubric.total_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        )
        if self.item_instance.rubric_id != self.rubric.rubric_id or not maximum_matches:
            raise DomainError(
                code="RUBRIC_ITEM_MISMATCH",
                module="m8",
                message="scoring task item and rubric must have matching identity and score",
                details={"scoring_task_id": self.scoring_task_id},
            )

    def max_score(self) -> float:
        """Return the teacher-approved rubric total for this task."""

        return self.rubric.total_score

    def answer_checksum(self) -> str:
        """Return a deterministic SHA-256 of the exact student answer text."""

        return hashlib.sha256(self.student_answer.encode("utf-8")).hexdigest()

    def criterion_ids(self) -> set[str]:
        """Return the independent set of criterion identifiers in the rubric."""

        return {criterion.criterion_id for criterion in self.rubric.criteria}


class ScoreAuditRecord(ContractModel):
    """Versioned M8 scoring record retained through teacher overrides."""

    audit_id: str = Field(min_length=1)
    audit_version: int = Field(ge=1)
    attempt_id: str = Field(min_length=1)
    item_instance_id: str = Field(min_length=1)
    criterion_scores: list[CriterionScore]
    total_score: float = Field(ge=0.0, allow_inf_nan=False)
    max_score: float = Field(ge=0.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    scoring_method: Literal[
        "rule",
        "local_model",
        "teacher_override",
        "local_model_rescore",
    ]
    review_status: str = Field(min_length=1)
    review_reason: list[str]
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Validate criterion identity, total conservation, and the score cap."""

        _require_unique(
            [criterion.criterion_id for criterion in self.criterion_scores],
            code="DUPLICATE_CRITERION_ID",
            message="criterion identifiers must be unique within an audit record",
            details={"audit_id": self.audit_id},
        )
        criterion_total = self.criterion_score_sum()
        if not math.isclose(
            criterion_total,
            self.total_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            raise DomainError(
                code="SCORE_TOTAL_MISMATCH",
                module="m8",
                message="criterion scores must equal the audit total",
                details={
                    "audit_id": self.audit_id,
                    "criterion_total": criterion_total,
                    "total_score": self.total_score,
                },
            )
        if self.total_score > self.max_score + _SCORE_TOLERANCE:
            raise DomainError(
                code="SCORE_LIMIT_EXCEEDED",
                module="m8",
                message="audit total must not exceed the item maximum",
                details={
                    "audit_id": self.audit_id,
                    "total_score": self.total_score,
                    "max_score": self.max_score,
                },
            )

    def criterion_score_sum(self) -> float:
        """Return a finite, numerically stable sum of criterion scores."""

        return _finite_score_sum(
            [criterion.score for criterion in self.criterion_scores],
            code="SCORE_TOTAL_MISMATCH",
            message="criterion scores must have a finite sum",
            details={"audit_id": self.audit_id},
        )

    def next_version(self) -> int:
        """Return the next immutable audit version number."""

        return self.audit_version + 1

    def needs_review(self) -> bool:
        """Return whether the current review state remains unresolved."""

        normalized_status = " ".join(self.review_status.split()).casefold()
        return normalized_status not in _COMPLETED_REVIEW_STATUSES

    def is_rejected(self) -> bool:
        """Return whether this version invalidates its score pending rescore."""

        normalized_status = " ".join(self.review_status.split()).casefold()
        return normalized_status in _REJECTED_REVIEW_STATUSES


class ScoringPreparationResult(ContractModel):
    """M8 split of completed rule audits and pending rubric tasks."""

    attempt_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    objective_audit_records: list[ScoreAuditRecord]
    objective_item_instances: list[ItemInstance] = Field(default_factory=list)
    rubric_scoring_tasks: list[RubricScoringTask]
    evidence_queries: list[EvidenceQuery]
    raw_answer_checksum: str = Field(min_length=1)
    prepared_at: datetime

    def validate_business_rules(self) -> None:
        """Require aligned attempt, paper, scoring-task, and query references."""

        _require_unique(
            [task.scoring_task_id for task in self.rubric_scoring_tasks],
            code="DUPLICATE_SCORING_TASK",
            message="rubric scoring task identifiers must be unique",
        )
        _require_unique(
            [query.query_id for query in self.evidence_queries],
            code="DUPLICATE_EVIDENCE_QUERY",
            message="scoring evidence query identifiers must be unique",
        )
        _require_unique(
            [record.item_instance_id for record in self.objective_audit_records],
            code="DUPLICATE_OBJECTIVE_AUDIT",
            message="rule-scored items must have one prepared audit record",
        )
        _require_unique(
            [item.item_instance_id for item in self.objective_item_instances],
            code="DUPLICATE_OBJECTIVE_AUDIT",
            message="rule-scored item contexts must be unique",
        )
        if self.objective_item_instances and (
            {
                item.item_instance_id for item in self.objective_item_instances
            }
            != {
                record.item_instance_id
                for record in self.objective_audit_records
            }
        ):
            raise DomainError(
                code="SCORING_REFERENCE_MISMATCH",
                module="m8",
                message="rule-scored item contexts must match prepared audits",
            )
        if any(
            record.attempt_id != self.attempt_id
            for record in self.objective_audit_records
        ) or any(
            task.attempt_id != self.attempt_id or task.paper_id != self.paper_id
            for task in self.rubric_scoring_tasks
        ):
            raise DomainError(
                code="SCORING_REFERENCE_MISMATCH",
                module="m8",
                message="prepared audits and tasks must reference the result attempt and paper",
            )

        query_by_id = {query.query_id: query for query in self.evidence_queries}
        task_query_ids = [
            task.evidence_query_id for task in self.rubric_scoring_tasks
        ]
        if len(task_query_ids) != len(set(task_query_ids)) or set(task_query_ids) != set(
            query_by_id
        ):
            raise DomainError(
                code="SCORING_QUERY_MISMATCH",
                module="m8",
                message="every rubric scoring task requires exactly one evidence query",
            )
        for task in self.rubric_scoring_tasks:
            query = query_by_id[task.evidence_query_id]
            if query.use_case != "grading" or query.item_id != task.item_instance.item_id:
                raise DomainError(
                    code="SCORING_QUERY_MISMATCH",
                    module="m8",
                    message="scoring query use case and item must match its task",
                    details={"scoring_task_id": task.scoring_task_id},
                )

    def pending_task_ids(self) -> list[str]:
        """Return pending rubric task identifiers in preparation order."""

        return [task.scoring_task_id for task in self.rubric_scoring_tasks]

    def query_for_task(self, scoring_task_id: str) -> EvidenceQuery:
        """Return the aligned evidence query as an independent deep copy."""

        for task in self.rubric_scoring_tasks:
            if task.scoring_task_id == scoring_task_id:
                for query in self.evidence_queries:
                    if query.query_id == task.evidence_query_id:
                        return query.model_copy(deep=True)
        raise DomainError(
            code="SCORING_TASK_NOT_FOUND",
            module="m8",
            message="rubric scoring task was not found",
            details={"scoring_task_id": scoring_task_id},
        )

    def all_objective_scored(self) -> bool:
        """Return whether every objective audit was produced by the rule scorer."""

        return all(
            record.scoring_method == "rule"
            for record in self.objective_audit_records
        )


class RubricScoringResult(ContractModel):
    """M7 criterion-level result returned to M8 for formal audit creation."""

    scoring_task_id: str = Field(min_length=1)
    criterion_scores: list[CriterionScore]
    total_score: float = Field(ge=0.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    missing_concept_ids: list[str]
    review_flags: list[str]
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    scored_at: datetime

    def validate_business_rules(self) -> None:
        """Validate unique criteria, result metadata, and score conservation."""

        _require_unique(
            [criterion.criterion_id for criterion in self.criterion_scores],
            code="DUPLICATE_CRITERION_ID",
            message="criterion identifiers must be unique within a scoring result",
            details={"scoring_task_id": self.scoring_task_id},
        )
        _require_unique(
            self.missing_concept_ids,
            code="DUPLICATE_SCORING_REFERENCE",
            message="missing concept identifiers must be unique",
        )
        _require_unique(
            self.review_flags,
            code="DUPLICATE_SCORING_REFERENCE",
            message="review flags must be unique",
        )
        criterion_total = self.criterion_score_sum()
        if not math.isclose(
            criterion_total,
            self.total_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            raise DomainError(
                code="SCORE_TOTAL_MISMATCH",
                module="m7",
                message="criterion scores must equal the rubric result total",
                details={
                    "scoring_task_id": self.scoring_task_id,
                    "criterion_total": criterion_total,
                    "total_score": self.total_score,
                },
            )

    def criterion_score_sum(self) -> float:
        """Return a finite, numerically stable sum of criterion scores."""

        return _finite_score_sum(
            [criterion.score for criterion in self.criterion_scores],
            code="SCORE_TOTAL_MISMATCH",
            message="criterion scores must have a finite sum",
            details={"scoring_task_id": self.scoring_task_id},
        )

    def requires_review(self, threshold: float) -> bool:
        """Route low-confidence or explicitly flagged output to review."""

        if (
            type(threshold) not in {int, float}
            or not 0.0 <= threshold <= 1.0
        ):
            raise DomainError(
                code="INVALID_REVIEW_THRESHOLD",
                module="m7",
                message="review threshold must be a finite probability",
                details={"threshold_type": type(threshold).__name__},
            )
        return self.confidence < threshold or bool(self.review_flags)

    def get_criterion(self, criterion_id: str) -> CriterionScore:
        """Return one criterion decision without exposing result state."""

        for criterion in self.criterion_scores:
            if criterion.criterion_id == criterion_id:
                return criterion.model_copy(deep=True)
        raise DomainError(
            code="CRITERION_NOT_FOUND",
            module="m7",
            message="criterion score was not found",
            details={"criterion_id": criterion_id},
        )


class RemediationTarget(ContractModel):
    """One concept or misconception target for subsequent practice."""

    concept_id: str = Field(min_length=1)
    misconception_id: str | None = Field(min_length=1)
    priority: int = Field(ge=1)
    recommended_item_ids: list[str]
    reason: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Require unambiguous recommended-item references."""

        _require_unique(
            self.recommended_item_ids,
            code="DUPLICATE_REMEDIATION_ITEM",
            message="recommended remediation item identifiers must be unique",
            details={"concept_id": self.concept_id},
        )

    def is_high_priority(self) -> bool:
        """Return whether this target occupies the highest priority rank."""

        return self.priority == 1


class RemediationPlan(ContractModel):
    """Attempt-bound ordered remediation targets produced by M8."""

    plan_id: str = Field(min_length=1)
    based_on_attempt_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    targets: list[RemediationTarget]
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Require one target for each concept/misconception identity."""

        _require_unique(
            [
                (target.concept_id, target.misconception_id)
                for target in self.targets
            ],
            code="DUPLICATE_REMEDIATION_TARGET",
            message="remediation target identities must be unique",
            details={"plan_id": self.plan_id},
        )

    def ordered_targets(self) -> list[RemediationTarget]:
        """Return stable priority order as independent deep copies."""

        ordered = sorted(self.targets, key=lambda target: target.priority)
        return [target.model_copy(deep=True) for target in ordered]

    def target_concept_ids(self) -> list[str]:
        """Return target concept identifiers in declared plan order."""

        return [target.concept_id for target in self.targets]


class ScoringResultBundle(ContractModel):
    """M8 score history with current totals and attempt-bound remediation."""

    attempt_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    score_audit_records: list[ScoreAuditRecord]
    learning_events: list[LearningEvent]
    remediation_plan: RemediationPlan
    total_score: float = Field(ge=0.0, allow_inf_nan=False)
    max_score: float = Field(ge=0.0, allow_inf_nan=False)
    finalized_at: datetime

    def validate_business_rules(self) -> None:
        """Validate history identity and current latest-version totals."""

        _require_unique(
            [
                (record.audit_id, record.audit_version)
                for record in self.score_audit_records
            ],
            code="DUPLICATE_AUDIT_VERSION",
            message="audit identity and version pairs must be unique",
        )
        if any(
            record.attempt_id != self.attempt_id
            for record in self.score_audit_records
        ):
            raise DomainError(
                code="AUDIT_REFERENCE_MISMATCH",
                module="m8",
                message="every audit record must reference the bundle attempt",
                details={"attempt_id": self.attempt_id},
            )

        _validate_audit_history(self.score_audit_records)
        _validate_learning_event_references(
            self.learning_events,
            attempt_id=self.attempt_id,
            learner_id=self.learner_id,
        )

        if (
            self.remediation_plan.based_on_attempt_id != self.attempt_id
            or self.remediation_plan.learner_id != self.learner_id
        ):
            raise DomainError(
                code="REMEDIATION_REFERENCE_MISMATCH",
                module="m8",
                message="remediation plan must reference the bundle attempt and learner",
            )

        current_total = self.recalculate_total()
        if not math.isclose(
            current_total,
            self.total_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            raise DomainError(
                code="BUNDLE_TOTAL_MISMATCH",
                module="m8",
                message="bundle total must equal the latest audit versions",
                details={
                    "attempt_id": self.attempt_id,
                    "audit_total": current_total,
                    "total_score": self.total_score,
                },
            )
        current_maximum = _finite_score_sum(
            [record.max_score for record in _latest_audit_records(self.score_audit_records)],
            code="BUNDLE_MAX_MISMATCH",
            message="latest audit maxima must have a finite total",
            details={"attempt_id": self.attempt_id},
        )
        if not math.isclose(
            current_maximum,
            self.max_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            raise DomainError(
                code="BUNDLE_MAX_MISMATCH",
                module="m8",
                message="bundle maximum must equal the latest audit maxima",
                details={
                    "attempt_id": self.attempt_id,
                    "audit_maximum": current_maximum,
                    "max_score": self.max_score,
                },
            )

    def get_audit_record(self, audit_id: str) -> ScoreAuditRecord:
        """Return the latest version of one audit as an independent copy."""

        matches = [
            record
            for record in self.score_audit_records
            if record.audit_id == audit_id
        ]
        if not matches:
            raise DomainError(
                code="AUDIT_NOT_FOUND",
                module="m8",
                message="score audit record was not found",
                details={"audit_id": audit_id},
            )
        latest = max(matches, key=lambda record: record.audit_version)
        return latest.model_copy(deep=True)

    def requires_teacher_review(self) -> bool:
        """Inspect only current audit versions for unresolved review work."""

        return any(
            record.needs_review()
            for record in _latest_audit_records(self.score_audit_records)
        )

    def has_rejected_score(self) -> bool:
        """Return whether any current audit is invalid pending rescore."""

        return any(
            record.is_rejected()
            for record in _latest_audit_records(self.score_audit_records)
        )

    def rejected_audit_ids(self) -> list[str]:
        """Return stable current audit identities that cannot be consumed."""

        return [
            record.audit_id
            for record in _latest_audit_records(self.score_audit_records)
            if record.is_rejected()
        ]

    def assert_score_usable(self, *, module: str) -> None:
        """Fail closed before a rejected historical score reaches consumers."""

        rejected = self.rejected_audit_ids()
        if rejected:
            raise DomainError(
                code="SCORE_REJECTED_PENDING_RESCORE",
                module=module,
                message=(
                    "rejected score evidence cannot be used before rescore "
                    "or a complete teacher override"
                ),
                details={"audit_ids": rejected},
                recoverable=True,
            )

    def replace_audit_record(self, record: ScoreAuditRecord) -> None:
        """Append one next version while preserving all prior audit history."""

        existing = [
            candidate
            for candidate in self.score_audit_records
            if candidate.audit_id == record.audit_id
        ]
        if not existing:
            raise DomainError(
                code="AUDIT_NOT_FOUND",
                module="m8",
                message="score audit record was not found",
                details={"audit_id": record.audit_id},
            )
        current = max(existing, key=lambda candidate: candidate.audit_version)
        valid_next_version = record.audit_version == current.next_version()
        references_match = (
            record.attempt_id == self.attempt_id == current.attempt_id
            and record.item_instance_id == current.item_instance_id
            and record.scoring_method in {
                "teacher_override",
                "local_model_rescore",
            }
            and (
                record.scoring_method != "local_model_rescore"
                or current.is_rejected()
            )
            and math.isclose(
                record.max_score,
                current.max_score,
                rel_tol=0.0,
                abs_tol=_SCORE_TOLERANCE,
            )
        )
        if not valid_next_version or not references_match:
            raise DomainError(
                code="REVIEW_VERSION_CONFLICT",
                module="m8",
                message="replacement audit must be the next aligned version",
                details={
                    "audit_id": record.audit_id,
                    "current_version": current.audit_version,
                    "replacement_version": record.audit_version,
                },
                recoverable=True,
            )

        # Build and validate a complete candidate before atomically replacing
        # fields; sequential validated assignment would observe a stale total.
        new_history = [
            candidate.model_copy(deep=True)
            for candidate in self.score_audit_records
        ]
        new_history.append(record.model_copy(deep=True))
        new_total = _finite_score_sum(
            [candidate.total_score for candidate in _latest_audit_records(new_history)],
            code="BUNDLE_TOTAL_MISMATCH",
            message="latest audit scores must have a finite total",
            details={"attempt_id": self.attempt_id},
        )
        new_maximum = _finite_score_sum(
            [candidate.max_score for candidate in _latest_audit_records(new_history)],
            code="BUNDLE_MAX_MISMATCH",
            message="latest audit maxima must have a finite total",
            details={"attempt_id": self.attempt_id},
        )
        candidate_bundle = type(self)(
            **{
                **self.model_dump(mode="python"),
                "score_audit_records": new_history,
                "total_score": new_total,
                "max_score": new_maximum,
            }
        )
        object.__setattr__(
            self,
            "score_audit_records",
            [
                candidate.model_copy(deep=True)
                for candidate in candidate_bundle.score_audit_records
            ],
        )
        object.__setattr__(self, "total_score", candidate_bundle.total_score)
        object.__setattr__(self, "max_score", candidate_bundle.max_score)

    def recalculate_total(self) -> float:
        """Return the total of only the latest version for each audit/item."""

        return _finite_score_sum(
            [
                record.total_score
                for record in _latest_audit_records(self.score_audit_records)
            ],
            code="BUNDLE_TOTAL_MISMATCH",
            message="latest audit scores must have a finite total",
            details={"attempt_id": self.attempt_id},
        )
