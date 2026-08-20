"""Governed M7 adapters for subjective rubric scoring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from course_insight.contracts.assessment import (
    CriterionScore,
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceBundle
from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMGenerationResult,
    LLMModelRef,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m7_local_model.prompts import (
    PromptEnvelope,
    SCORING_PROMPT_ID,
    SCORING_PROMPT_VERSION,
    prepare_scoring_prompt,
)
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.privacy import (
    DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
    M7OutboundPrivacyPolicy,
    OutboundPrivacyResult,
)
from course_insight.modules.m7_local_model.privacy_reviewer import (
    DenyAllPrivacyReviewer,
    PrivacyReviewer,
)
from course_insight.modules.m7_local_model.review_selection import (
    AllReviewSelector,
    IsotonicReviewSelector,
    ReviewSelectionDecision,
    ReviewSelector,
    select_teacher_review,
)


@dataclass(frozen=True)
class M7ScoringOutcome:
    """Validated score plus privacy-safe DeepSeek execution metadata."""

    result: RubricScoringResult
    audit: ModelInvocationAudit
    safety: SafetyCheckResult
    prompt_record: dict[str, Any]
    student_answer_for_validation: str
    review_selection: ReviewSelectionDecision


class M7InvocationFailure(Exception):
    """Carry sanitized audit data back to the service on a closed failure."""

    def __init__(
        self,
        *,
        error: DomainError,
        audit: ModelInvocationAudit,
        safety: SafetyCheckResult,
        prompt_record: dict[str, Any],
    ) -> None:
        self.error = error
        self.audit = audit
        self.safety = safety
        self.prompt_record = dict(prompt_record)
        super().__init__(str(error))


@runtime_checkable
class RubricScoringAdapter(Protocol):
    """Network-free or legacy rubric-scoring boundary."""

    def score(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> RubricScoringResult:
        """Return one rubric score."""


@runtime_checkable
class GovernedRubricScoringAdapter(Protocol):
    """Real governed rubric-scoring boundary with safe audit outcomes."""

    def score_governed(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> M7ScoringOutcome:
        """Return a governed rubric-scoring outcome."""


GovernedM7Adapter = GovernedRubricScoringAdapter


class PlaceholderRubricAdapter:
    """Expose the rubric adapter interface until a scorer is configured."""

    def score(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> RubricScoringResult:
        """Fail explicitly until a governed scoring implementation is supplied."""

        del task, evidence_bundle
        raise DomainError(
            code="MODEL_ADAPTER_UNCONFIGURED",
            module="m7",
            message="no rubric scoring adapter is configured",
            recoverable=True,
        )


class DeepSeekM7Adapter:
    """Translate M7 contracts to and from governed DeepSeek JSON output."""

    def __init__(
        self,
        client: DeepSeekClient,
        policy: M7ExecutionPolicy = DEFAULT_M7_EXECUTION_POLICY,
        privacy_policy: M7OutboundPrivacyPolicy = (
            DEFAULT_M7_OUTBOUND_PRIVACY_POLICY
        ),
        privacy_reviewer: PrivacyReviewer | None = None,
        review_selector: ReviewSelector | None = None,
    ) -> None:
        client_policy = client.policy
        if (
            client_policy.model_name != policy.model_name
            or client_policy.model_version != policy.model_version
            or client_policy.thinking_enabled != policy.thinking_enabled
            or client_policy.temperature != policy.temperature
            or client_policy.max_tokens != policy.max_tokens
        ):
            raise ValueError(
                "DeepSeek client settings do not match the frozen M7 policy"
            )
        self._client = client
        self._policy = policy
        self._privacy_policy = privacy_policy
        self._privacy_reviewer = (
            privacy_reviewer
            if privacy_reviewer is not None
            else DenyAllPrivacyReviewer(
                reason_code="privacy_reviewer_unconfigured",
                reviewer_id="privacy-unconfigured",
            )
        )
        self._review_selector = (
            review_selector
            if review_selector is not None
            else AllReviewSelector(
                configured_mode=policy.review_selection_mode,
                fallback=policy.review_selection_mode != "all_review",
            )
        )
        if isinstance(self._review_selector, IsotonicReviewSelector):
            binding = self._review_selector.binding
            expected = (
                policy.model_name,
                policy.model_version,
                policy.model_candidate.thinking_mode,
                SCORING_PROMPT_ID,
                SCORING_PROMPT_VERSION,
                policy.policy_version,
                privacy_policy.policy_version,
                policy.review_feature_schema_version,
            )
            actual = (
                binding.model_name,
                binding.model_version,
                binding.thinking_mode,
                binding.prompt_id,
                binding.prompt_version,
                binding.execution_policy_version,
                binding.privacy_policy_version,
                binding.feature_schema_version,
            )
            if actual != expected:
                raise ValueError("review selector binding does not match M7 runtime")

    def score_governed(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> M7ScoringOutcome:
        """Generate and validate one evidence-bound rubric score."""

        prepared = prepare_scoring_prompt(
            task,
            evidence_bundle,
            self._policy,
            self._privacy_policy,
            privacy_reviewer=self._privacy_reviewer,
        )
        if prepared.prompt is None or prepared.governed_task is None:
            self._privacy_blocked(task, prepared.privacy)
        assert prepared.prompt is not None
        assert prepared.governed_task is not None
        prompt = prepared.prompt
        governed_task = prepared.governed_task
        request = self._request(
            request_id=(
                f"m7_score_{task.scoring_task_id}_"
                f"{prompt.input_checksum[:12]}"
            ),
            use_case="rubric_scoring",
            prompt=prompt,
            created_at=governed_task.created_at,
        )
        invocation = self._client.invoke_json(
            request=request,
            messages=prompt.messages,
        )
        prompt_record = {
            **prompt.safe_record(
                request_id=request.request_id,
                use_case=request.use_case,
            ),
            "scoring_task_id": governed_task.scoring_task_id,
            "model_version": self._client.model_version,
            **prepared.privacy.safe_record(),
        }
        privacy_flags = _privacy_safety_flags(prepared.privacy)
        self._require_success(
            invocation.result,
            invocation.audit,
            request=request,
            prompt_record=prompt_record,
            privacy_flags=privacy_flags,
        )
        try:
            result = _parse_scoring_result(
                task=governed_task,
                evidence_bundle=evidence_bundle,
                generation=invocation.result,
                model_name=self._client.model_name,
                model_version=self._client.model_version,
                policy=self._policy,
            )
            review_selection = select_teacher_review(
                self._review_selector,
                task=governed_task,
                evidence_bundle=evidence_bundle,
                result=result,
                privacy=prepared.privacy,
                configured_mode=self._policy.review_selection_mode,
            )
            result = result.model_copy(
                update={"review_flags": list(review_selection.review_flags)},
                deep=True,
            )
        except (DomainError, ValidationError, TypeError, ValueError) as error:
            self._invalid_output(
                request=request,
                generation=invocation.result,
                audit=invocation.audit,
                prompt_record=prompt_record,
                privacy_flags=privacy_flags,
                cause=error,
            )
        prompt_record.update(review_selection.safe_record())
        safety_flags = list(
            dict.fromkeys(
                [
                    *result.review_flags,
                    *review_selection.audit_flags,
                    *privacy_flags,
                ]
            )
        )
        safety = SafetyCheckResult(
            request_id=request.request_id,
            status="passed",
            flags=safety_flags,
            checked_at=invocation.result.generated_at,
        )
        return M7ScoringOutcome(
            result=result,
            audit=invocation.audit,
            safety=safety,
            prompt_record=prompt_record,
            student_answer_for_validation=governed_task.student_answer,
            review_selection=review_selection,
        )

    def _request(
        self,
        *,
        request_id: str,
        use_case: str,
        prompt: PromptEnvelope,
        created_at: Any,
    ) -> LLMGenerationRequest:
        return LLMGenerationRequest(
            request_id=request_id,
            use_case=use_case,
            model_ref=LLMModelRef(
                provider="deepseek",
                model_name=self._client.model_name,
                model_version=self._client.model_version,
                api_key_env="DEEPSEEK_API_KEY",
                status="configured",
            ),
            prompt_template_id=prompt.prompt_id,
            prompt_template_version=prompt.prompt_version,
            evidence_ids=list(prompt.evidence_ids),
            input_checksum=prompt.input_checksum,
            created_at=created_at,
        )

    def _require_success(
        self,
        generation: LLMGenerationResult,
        audit: ModelInvocationAudit,
        *,
        request: LLMGenerationRequest,
        prompt_record: dict[str, Any],
        privacy_flags: list[str],
    ) -> None:
        if generation.status == "succeeded":
            return
        blocked = generation.status == "blocked"
        safety = SafetyCheckResult(
            request_id=request.request_id,
            status=("blocked" if blocked else "not_run"),
            flags=[
                *privacy_flags,
                *(["provider_content_filter"] if blocked else []),
            ],
            checked_at=generation.generated_at,
        )
        error_code = audit.error_code or "DEEPSEEK_INVOCATION_FAILED"
        if error_code in {
            "DEEPSEEK_API_KEY_MISSING",
            "DEEPSEEK_MODEL_UNCONFIGURED",
            "DEEPSEEK_MODEL_MISMATCH",
        }:
            domain_code = "MODEL_ADAPTER_UNCONFIGURED"
            message = "the governed DeepSeek adapter is not configured"
        elif blocked:
            domain_code = "MODEL_OUTPUT_BLOCKED"
            message = "DeepSeek blocked the governed generation request"
        else:
            domain_code = "MODEL_API_UNAVAILABLE"
            message = "DeepSeek generation is temporarily unavailable"
        raise M7InvocationFailure(
            error=DomainError(
                code=domain_code,
                module="m7",
                message=message,
                details={
                    "request_id": request.request_id,
                    "provider_error_code": error_code,
                },
                recoverable=True,
            ),
            audit=audit,
            safety=safety,
            prompt_record=prompt_record,
        )

    @staticmethod
    def _invalid_output(
        *,
        request: LLMGenerationRequest,
        generation: LLMGenerationResult,
        audit: ModelInvocationAudit,
        prompt_record: dict[str, Any],
        privacy_flags: list[str],
        cause: Exception,
    ) -> None:
        del cause
        raise M7InvocationFailure(
            error=DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="DeepSeek returned output that violates the M7 contract",
                details={"request_id": request.request_id},
                recoverable=True,
            ),
            audit=audit,
            safety=SafetyCheckResult(
                request_id=request.request_id,
                status="blocked",
                flags=[*privacy_flags, "invalid_model_output"],
                checked_at=generation.generated_at,
            ),
            prompt_record=prompt_record,
        )

    def _privacy_blocked(
        self,
        task: RubricScoringTask,
        privacy: OutboundPrivacyResult,
    ) -> None:
        """Stop before prompt construction or network access and retain metadata."""

        request_id = (
            f"m7_score_{task.scoring_task_id}_privacy_"
            f"{privacy.input_checksum[:12]}"
        )
        flags = ["outbound_privacy_blocked", *privacy.flags]
        prompt_record = {
            "request_id": request_id,
            "use_case": "rubric_scoring",
            "scoring_task_id": task.scoring_task_id,
            "model_version": self._client.model_version,
            "prompt_template_id": SCORING_PROMPT_ID,
            "prompt_template_version": SCORING_PROMPT_VERSION,
            "execution_policy_version": self._policy.policy_version,
            "input_checksum": None,
            "evidence_ids": [],
            **privacy.safe_record(),
        }
        raise M7InvocationFailure(
            error=DomainError(
                code="MODEL_INPUT_PRIVACY_BLOCKED",
                module="m7",
                message=(
                    "student answer is unsafe for third-party model processing"
                ),
                details={
                    "privacy_policy_version": privacy.policy_version,
                    "privacy_flags": list(privacy.flags),
                },
                recoverable=True,
            ),
            audit=ModelInvocationAudit(
                invocation_id=f"invocation_{request_id}",
                request_id=request_id,
                provider="deepseek",
                model_name=self._client.model_name,
                status="not_run",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                error_code="OUTBOUND_PRIVACY_BLOCKED",
                created_at=task.created_at,
            ),
            safety=SafetyCheckResult(
                request_id=request_id,
                status="blocked",
                flags=flags,
                checked_at=task.created_at,
            ),
            prompt_record=prompt_record,
        )


def _parse_scoring_result(
    *,
    task: RubricScoringTask,
    evidence_bundle: EvidenceBundle,
    generation: LLMGenerationResult,
    model_name: str,
    model_version: str,
    policy: M7ExecutionPolicy,
) -> RubricScoringResult:
    data = generation.structured_output
    _require_exact_keys(
        data,
        {
            "criterion_scores",
            "total_score",
            "confidence",
            "missing_concept_ids",
            "review_flags",
            "citation_ids",
        },
    )
    raw_scores = data["criterion_scores"]
    if type(raw_scores) is not list:
        raise ValueError
    criterion_scores: list[CriterionScore] = []
    for raw_score in raw_scores:
        _require_exact_keys(
            raw_score,
            {
                "criterion_id",
                "score",
                "student_evidence",
                "course_evidence_id",
                "reason",
            },
        )
        reason = _text(raw_score["reason"])
        if len(reason) > policy.max_scoring_reason_characters:
            raise ValueError
        criterion_scores.append(
            CriterionScore(
                criterion_id=_text(raw_score["criterion_id"]),
                score=_number(raw_score["score"]),
                student_evidence=_string(raw_score["student_evidence"]),
                course_evidence_id=_optional_text(
                    raw_score["course_evidence_id"]
                ),
                reason=reason,
            )
        )

    expected_ids = task.criterion_ids()
    actual_ids = {score.criterion_id for score in criterion_scores}
    if (
        len(criterion_scores) != len(actual_ids)
        or actual_ids != expected_ids
    ):
        raise ValueError
    available_ids = set(evidence_bundle.citation_ids())
    declared_citations = set(generation.citation_ids)
    if not declared_citations <= available_ids:
        raise ValueError

    for score in criterion_scores:
        criterion = task.rubric.criterion(score.criterion_id)
        if score.score > criterion.max_score + 1e-9:
            raise ValueError
        if (
            score.student_evidence
            and score.student_evidence not in task.student_answer
        ):
            raise ValueError
        if score.course_evidence_id is not None and (
            score.course_evidence_id not in available_ids
            or score.course_evidence_id not in criterion.course_evidence_ids
            or score.course_evidence_id not in declared_citations
        ):
            raise ValueError
        if (
            score.score > 0.0
            and task.rubric.review_policy.require_evidence_for_positive_score
            and score.course_evidence_id is None
        ):
            raise ValueError

    used_citations = list(
        dict.fromkeys(
            score.course_evidence_id
            for score in criterion_scores
            if score.course_evidence_id is not None
        )
    )
    if generation.citation_ids != used_citations:
        raise ValueError

    total_score = _number(data["total_score"])
    calculated_total = math.fsum(score.score for score in criterion_scores)
    if (
        not math.isclose(
            total_score,
            calculated_total,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        or total_score > task.max_score() + 1e-9
    ):
        raise ValueError
    confidence = _probability(data["confidence"])
    missing_ids = _text_list(data["missing_concept_ids"])
    if not set(missing_ids) <= set(task.item_instance.concept_ids):
        raise ValueError
    review_flags = _text_list(data["review_flags"])
    if review_flags:
        raise ValueError

    return RubricScoringResult(
        scoring_task_id=task.scoring_task_id,
        criterion_scores=criterion_scores,
        total_score=total_score,
        confidence=confidence,
        missing_concept_ids=missing_ids,
        review_flags=review_flags,
        model_name=model_name,
        model_version=model_version,
        scored_at=generation.generated_at,
    )


def _privacy_safety_flags(result: OutboundPrivacyResult) -> list[str]:
    return [f"outbound_privacy_{result.decision}", *result.flags]


def _require_exact_keys(
    value: Any,
    expected: set[str],
) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError
    return value.strip()


def _string(value: Any) -> str:
    if type(value) is not str:
        raise ValueError
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _text(value)


def _number(value: Any) -> float:
    if type(value) not in {int, float}:
        raise ValueError
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError
    return number


def _probability(value: Any) -> float:
    number = _number(value)
    if number > 1.0:
        raise ValueError
    return number


def _text_list(value: Any) -> list[str]:
    if type(value) is not list:
        raise ValueError
    result = [_text(item) for item in value]
    if len(result) != len(set(result)):
        raise ValueError
    return result


__all__ = [
    "DeepSeekM7Adapter",
    "GovernedM7Adapter",
    "GovernedRubricScoringAdapter",
    "M7InvocationFailure",
    "M7ScoringOutcome",
    "PlaceholderRubricAdapter",
    "RubricScoringAdapter",
]
