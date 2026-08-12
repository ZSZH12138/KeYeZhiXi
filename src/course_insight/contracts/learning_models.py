"""M5, M8, and M9 contracts for learning-model architecture placeholders."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError


class LearningObservation(ContractModel):
    """One immutable, pseudonymous response observation consumed by M5/M8."""

    observation_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    item_version: str = Field(min_length=1)
    concept_ids: list[str] = Field(min_length=1)
    score: float = Field(ge=0.0, allow_inf_nan=False)
    max_score: float = Field(gt=0.0, allow_inf_nan=False)
    response_outcome: Literal["correct", "incorrect"]
    outcome_policy_version: str = Field(min_length=1)
    source_audit_id: str = Field(min_length=1)
    source_audit_version: int = Field(ge=1)
    occurred_at: datetime

    def validate_business_rules(self) -> None:
        """Keep scores bounded and concept links unique."""

        if self.score > self.max_score or len(self.concept_ids) != len(
            set(self.concept_ids)
        ):
            raise DomainError(
                code="LEARNING_OBSERVATION_INVALID",
                module="m5",
                message="observation score and concept references must be valid",
            )


class LearningObservationBatch(ContractModel):
    """Ordered observation batch with a replay watermark."""

    batch_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    observations: list[LearningObservation]
    watermark: str = Field(min_length=1)
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Reject duplicate observations in one replay batch."""

        identities = [item.observation_id for item in self.observations]
        wrong_learner = any(
            item.learner_id != self.learner_id for item in self.observations
        )
        if len(identities) != len(set(identities)) or wrong_learner:
            raise DomainError(
                code="OBSERVATION_BATCH_INVALID",
                module="m5",
                message="observation identifiers and learner scope must be consistent",
            )


class DinaItemParameters(ContractModel):
    """DINA slip and guess estimates for one immutable item version."""

    item_id: str = Field(min_length=1)
    item_version: str = Field(min_length=1)
    concept_ids: list[str] = Field(min_length=1)
    slip: float = Field(ge=0.01, le=0.40, allow_inf_nan=False)
    guess: float = Field(ge=0.01, le=0.40, allow_inf_nan=False)
    sample_size: int = Field(ge=1)

    def validate_business_rules(self) -> None:
        """Require a unique, non-empty conjunctive concept set."""

        if len(self.concept_ids) != len(set(self.concept_ids)):
            raise DomainError(
                code="DINA_ITEM_PARAMETERS_INVALID",
                module="m5",
                message="DINA item concepts must be unique",
            )


class DinaModelArtifact(ContractModel):
    """Versioned fitted DINA model for one course and class scope."""

    model_id: str = Field(min_length=1)
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    concept_ids: list[str] = Field(min_length=1)
    item_parameters: list[DinaItemParameters] = Field(min_length=1)
    attribute_priors: dict[str, float]
    learner_count: int = Field(ge=1)
    observation_count: int = Field(ge=1)
    inference_mode: Literal["exact", "variational"]
    log_likelihood: float = Field(allow_inf_nan=False)
    elbo: float | None = Field(default=None, allow_inf_nan=False)
    objective_history: list[float] = Field(default_factory=list)
    iteration_count: int = Field(ge=1)
    converged: bool
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Keep concept, item, and inference metadata internally aligned."""

        concept_set = set(self.concept_ids)
        item_keys = [
            (item.item_id, item.item_version) for item in self.item_parameters
        ]
        invalid_priors = (
            set(self.attribute_priors) != concept_set
            or any(not 0.0 <= value <= 1.0 for value in self.attribute_priors.values())
        )
        invalid_items = any(
            not set(item.concept_ids) <= concept_set for item in self.item_parameters
        )
        invalid_mode = self.inference_mode == "variational" and (
            self.elbo is None
            or not self.objective_history
            or any(
                later + 1e-9 < earlier
                for earlier, later in zip(
                    self.objective_history,
                    self.objective_history[1:],
                    strict=False,
                )
            )
        )
        if (
            len(self.concept_ids) != len(concept_set)
            or len(item_keys) != len(set(item_keys))
            or invalid_priors
            or invalid_items
            or invalid_mode
        ):
            raise DomainError(
                code="DINA_MODEL_INVALID",
                module="m5",
                message="DINA model concepts, items, or inference metadata are invalid",
            )


class CognitiveDiagnosisResult(ContractModel):
    """Versioned DINA-family output; empty until the engine is configured."""

    run_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    model_type: Literal["DINA", "DINO", "GDINA", "NCDM"]
    model_version: str = Field(min_length=1)
    concept_mastery: dict[str, float]
    observation_count: int = Field(ge=0)
    status: Literal["empty", "estimated", "failed"]
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Keep probabilities finite and empty status semantically empty."""

        invalid = any(not 0.0 <= value <= 1.0 for value in self.concept_mastery.values())
        if invalid or (self.status == "empty" and self.concept_mastery):
            raise DomainError(
                code="COGNITIVE_DIAGNOSIS_INVALID",
                module="m5",
                message="diagnosis probabilities or empty status are invalid",
            )


class KnowledgeTraceSnapshot(ContractModel):
    """Versioned BKT-family trace; empty until sequence data is available."""

    trace_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    model_type: Literal["BKT", "BKT_FORGETTING", "DKT"]
    model_version: str = Field(min_length=1)
    concept_probabilities: dict[str, float]
    observation_watermark: str = Field(min_length=1)
    observation_count: int = Field(ge=0)
    status: Literal["empty", "estimated", "failed"]
    updated_at: datetime

    def validate_business_rules(self) -> None:
        """Keep probabilities finite and empty status semantically empty."""

        invalid = any(
            not 0.0 <= value <= 1.0
            for value in self.concept_probabilities.values()
        )
        if invalid or (self.status == "empty" and self.concept_probabilities):
            raise DomainError(
                code="KNOWLEDGE_TRACE_INVALID",
                module="m5",
                message="trace probabilities or empty status are invalid",
            )


class LearningModelRun(ContractModel):
    """Atomic M5 DINA and BKT placeholder run."""

    run_id: str = Field(min_length=1)
    diagnosis: CognitiveDiagnosisResult
    knowledge_trace: KnowledgeTraceSnapshot
    observation_count: int = Field(ge=0)
    status: Literal["empty", "completed", "failed"]
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Require component counts and statuses to match the run."""

        statuses = (self.diagnosis.status, self.knowledge_trace.status)
        status_is_consistent = (
            (self.status == "empty" and statuses == ("empty", "empty"))
            or (
                self.status == "completed"
                and statuses == ("estimated", "estimated")
            )
            or (self.status == "failed" and "failed" in statuses)
        )
        if (
            self.diagnosis.learner_id != self.knowledge_trace.learner_id
            or self.observation_count != self.diagnosis.observation_count
            or self.observation_count != self.knowledge_trace.observation_count
            or not status_is_consistent
        ):
            raise DomainError(
                code="LEARNING_MODEL_RUN_INVALID",
                module="m5",
                message="learning-model component identities and states must align",
            )


class IRTItemParameters(ContractModel):
    """Versioned IRT parameters for one immutable item version."""

    item_id: str = Field(min_length=1)
    item_version: str = Field(min_length=1)
    discrimination: float = Field(gt=0.0, allow_inf_nan=False)
    difficulty: float = Field(allow_inf_nan=False)
    guessing: float = Field(ge=0.0, lt=1.0, allow_inf_nan=False)
    sample_size: int = Field(ge=1)


class IRTParameterSet(ContractModel):
    """M8-owned append-only IRT parameter snapshot."""

    parameter_set_id: str = Field(min_length=1)
    model_type: Literal["1PL", "2PL", "3PL"]
    version: str = Field(min_length=1)
    item_parameters: list[IRTItemParameters]
    sample_size: int = Field(ge=0)
    status: Literal["empty", "shadow", "approved", "rejected"]
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Require unique item versions and genuinely empty empty sets."""

        identities = [
            (item.item_id, item.item_version) for item in self.item_parameters
        ]
        if len(identities) != len(set(identities)) or (
            self.status == "empty"
            and (self.item_parameters or self.sample_size != 0)
        ):
            raise DomainError(
                code="IRT_PARAMETER_SET_INVALID",
                module="m8",
                message="IRT item identities and empty status must be consistent",
            )


class AbilityEstimate(ContractModel):
    """Learner ability estimate bound to one IRT parameter set."""

    estimate_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    parameter_set_id: str = Field(min_length=1)
    theta: float | None = Field(default=None, allow_inf_nan=False)
    standard_error: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    status: Literal["empty", "estimated", "failed"]
    estimated_at: datetime

    def validate_business_rules(self) -> None:
        """Require estimates only for the estimated state."""

        has_all_values = self.theta is not None and self.standard_error is not None
        has_any_value = self.theta is not None or self.standard_error is not None
        if (self.status == "estimated" and not has_all_values) or (
            self.status != "estimated" and has_any_value
        ):
            raise DomainError(
                code="ABILITY_ESTIMATE_INVALID",
                module="m8",
                message="ability values must match the estimate status",
            )


class CalibrationRunResult(ContractModel):
    """M8 shadow-calibration result awaiting M9 review."""

    run_id: str = Field(min_length=1)
    parameter_set: IRTParameterSet
    converged: bool
    metrics: dict[str, float]
    status: Literal["empty", "shadow", "failed"]
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Prevent an empty run from claiming convergence or metrics."""

        if self.status == "empty" and (
            self.converged or self.metrics or self.parameter_set.status != "empty"
        ):
            raise DomainError(
                code="CALIBRATION_RUN_INVALID",
                module="m8",
                message="empty calibration runs cannot contain estimates",
            )


class AdaptiveSelectionPolicy(ContractModel):
    """M8 constraints for future IRT-information item selection."""

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    parameter_set_id: str | None = None
    max_items: int = Field(ge=1)
    concept_quotas: dict[str, int]
    status: Literal["empty", "configured"]

    def validate_business_rules(self) -> None:
        """Require non-negative quotas and no parameter set for empty policy."""

        if any(value < 0 for value in self.concept_quotas.values()) or (
            self.status == "empty" and self.parameter_set_id is not None
        ):
            raise DomainError(
                code="ADAPTIVE_POLICY_INVALID",
                module="m8",
                message="adaptive policy quotas or empty state are invalid",
            )


class AdaptiveSelectionResult(ContractModel):
    """M8 item selection output; empty until IRT is configured."""

    selection_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    item_ids: list[str]
    ability_estimate: AbilityEstimate | None = None
    status: Literal["empty", "selected", "failed"]
    selected_at: datetime

    def validate_business_rules(self) -> None:
        """Keep item identities unique and empty status empty."""

        if len(self.item_ids) != len(set(self.item_ids)) or (
            self.status == "empty"
            and (self.item_ids or self.ability_estimate is not None)
        ):
            raise DomainError(
                code="ADAPTIVE_SELECTION_INVALID",
                module="m8",
                message="adaptive selection identities or empty state are invalid",
            )


class ModelQualityReport(ContractModel):
    """M9 evidence gate for a model or parameter version."""

    report_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    metrics: dict[str, float]
    observation_count: int = Field(ge=0)
    status: Literal["insufficient_data", "ready", "failed"]
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Keep insufficient-data reports free of fabricated metrics."""

        if self.status == "insufficient_data" and self.metrics:
            raise DomainError(
                code="MODEL_QUALITY_REPORT_INVALID",
                module="m9",
                message="insufficient-data reports cannot contain quality metrics",
            )


class CalibrationReviewDecision(ContractModel):
    """M9 teacher decision for an M8 shadow calibration run."""

    decision_id: str = Field(min_length=1)
    calibration_run_id: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    decision: Literal["approve", "reject", "defer"]
    target_parameter_version: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    reviewed_at: datetime
