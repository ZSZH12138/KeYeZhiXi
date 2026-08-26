"""M2, M7, and M9 contracts for RAG and the DeepSeek API boundary."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.learning_models import (
    AdaptiveSelectionResult,
    CalibrationRunResult,
    LearningModelRun,
    ModelQualityReport,
)
from course_insight.contracts.platform import AsyncJobStatus


class EmbeddingModelRef(ContractModel):
    """Portable identity of a future embedding model used by M2."""

    provider: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    dimension: int | None = Field(default=None, ge=1)
    status: Literal["empty", "configured"]

    def validate_business_rules(self) -> None:
        """Require an embedding dimension only for configured models."""

        if (self.status == "configured") != (self.dimension is not None):
            raise DomainError(
                code="EMBEDDING_MODEL_REF_INVALID",
                module="m2",
                message="embedding dimension must match configuration status",
            )


class RetrievalPolicy(ContractModel):
    """Versioned M2 lexical/vector/hybrid retrieval policy."""

    policy_id: str = Field(min_length=1)
    strategy: Literal["lexical", "vector", "hybrid"]
    top_k: int = Field(ge=1)
    lexical_weight: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    vector_weight: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rerank: bool

    def validate_business_rules(self) -> None:
        """Require strategy-compatible, normalized retrieval weights."""

        if self.lexical_weight + self.vector_weight <= 0.0:
            raise DomainError(
                code="RETRIEVAL_POLICY_INVALID",
                module="m2",
                message="retrieval policy requires a positive signal weight",
            )
        if self.strategy == "hybrid" and not math.isclose(
            self.lexical_weight + self.vector_weight,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise DomainError(
                code="RETRIEVAL_POLICY_INVALID",
                module="m2",
                message="hybrid retrieval policy weights must sum to one",
            )


class RetrievalAudit(ContractModel):
    """M2 retrieval trace without raw query text or host paths."""

    audit_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    index_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    retrieved_evidence_ids: list[str]
    status: Literal["empty", "succeeded", "failed"]
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Require unique evidence IDs and no evidence for empty runs."""

        if len(self.retrieved_evidence_ids) != len(
            set(self.retrieved_evidence_ids)
        ) or (self.status == "empty" and self.retrieved_evidence_ids):
            raise DomainError(
                code="RETRIEVAL_AUDIT_INVALID",
                module="m2",
                message="retrieval audit evidence IDs or empty state are invalid",
            )


class LLMModelRef(ContractModel):
    """DeepSeek-only model declaration with an environment-key name."""

    provider: Literal["deepseek"] = "deepseek"
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    api_key_env: Literal["DEEPSEEK_API_KEY"] = "DEEPSEEK_API_KEY"
    status: Literal["empty", "configured"]


class LLMGenerationRequest(ContractModel):
    """Hashed, evidence-scoped request for M7 or M9 DeepSeek generation."""

    request_id: str = Field(min_length=1)
    use_case: Literal[
        "rubric_scoring",
        "student_feedback",
        "teacher_narrative",
        "knowledge_extraction",
        "question_concept_linking",
        "student_rag_qa",
    ]
    model_ref: LLMModelRef
    prompt_template_id: str = Field(min_length=1)
    prompt_template_version: str = Field(min_length=1)
    evidence_ids: list[str]
    input_checksum: str = Field(min_length=1)
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Reject duplicated governed evidence links."""

        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise DomainError(
                code="LLM_EVIDENCE_DUPLICATED",
                module="m7",
                message="LLM evidence identifiers must be unique",
            )


class LLMGenerationResult(ContractModel):
    """Structured DeepSeek output or an explicit empty placeholder."""

    request_id: str = Field(min_length=1)
    provider: Literal["deepseek"] = "deepseek"
    status: Literal["empty", "succeeded", "failed", "blocked"]
    content: str
    structured_output: dict[str, Any]
    citation_ids: list[str]
    finish_reason: Literal["not_run", "stop", "error", "blocked"]
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Prevent an empty placeholder from carrying generated content."""

        has_output = bool(self.content or self.structured_output)
        empty_is_consistent = self.status != "empty" or (
            not has_output
            and not self.citation_ids
            and self.finish_reason == "not_run"
        )
        success_is_consistent = self.status != "succeeded" or (
            has_output and self.finish_reason == "stop"
        )
        failed_is_consistent = self.status != "failed" or (
            not has_output
            and not self.citation_ids
            and self.finish_reason == "error"
        )
        blocked_is_consistent = self.status != "blocked" or (
            not has_output
            and not self.citation_ids
            and self.finish_reason == "blocked"
        )
        if (
            len(self.citation_ids) != len(set(self.citation_ids))
            or not empty_is_consistent
            or not success_is_consistent
            or not failed_is_consistent
            or not blocked_is_consistent
        ):
            raise DomainError(
                code="LLM_GENERATION_RESULT_INVALID",
                module="m7",
                message="DeepSeek result content and status are inconsistent",
            )


class ModelInvocationAudit(ContractModel):
    """Privacy-safe M7/M9 model-call audit metadata."""

    invocation_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    provider: Literal["deepseek"] = "deepseek"
    model_name: str = Field(min_length=1)
    status: Literal["not_run", "succeeded", "failed", "blocked"]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    error_code: str | None = None
    created_at: datetime


class SafetyCheckResult(ContractModel):
    """M7/M9 safety filter outcome without retaining prompt content."""

    request_id: str = Field(min_length=1)
    status: Literal["not_run", "passed", "blocked"]
    flags: list[str]
    checked_at: datetime


class ArchitectureScaffoldResult(ContractModel):
    """End-to-end empty result proving the v2 architecture is wired."""

    status: Literal["empty"] = "empty"
    web_job_status: AsyncJobStatus
    vector_index: EvidenceIndexRef
    retrieval_audit: RetrievalAudit
    scoring_generation: LLMGenerationResult
    teacher_generation: LLMGenerationResult
    learning_model_run: LearningModelRun
    calibration_result: CalibrationRunResult
    adaptive_selection_result: AdaptiveSelectionResult
    model_quality_report: ModelQualityReport
    completed_at: datetime

    def validate_business_rules(self) -> None:
        """Require every component to remain explicitly empty or skipped."""

        if not self.is_empty():
            raise DomainError(
                code="ARCHITECTURE_SCAFFOLD_NOT_EMPTY",
                module="application",
                message="architecture scaffold components must remain empty",
            )

    def is_empty(self) -> bool:
        """Return whether every v2 placeholder has its expected empty state."""

        return (
            self.web_job_status.status == "skipped"
            and self.web_job_status.progress == 0.0
            and self.web_job_status.result_ref is None
            and self.vector_index.status == "empty"
            and self.vector_index.backend == "pgvector"
            and self.vector_index.source_count == 0
            and self.vector_index.chunk_count == 0
            and self.vector_index.embedding_model_id is None
            and self.retrieval_audit.status == "empty"
            and not self.retrieval_audit.retrieved_evidence_ids
            and self.scoring_generation.status == "empty"
            and self.teacher_generation.status == "empty"
            and self.learning_model_run.status == "empty"
            and self.learning_model_run.observation_count == 0
            and self.calibration_result.status == "empty"
            and not self.calibration_result.metrics
            and self.adaptive_selection_result.status == "empty"
            and not self.adaptive_selection_result.item_ids
            and self.model_quality_report.status == "insufficient_data"
            and not self.model_quality_report.metrics
        )
