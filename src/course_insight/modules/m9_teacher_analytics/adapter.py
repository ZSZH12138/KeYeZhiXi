"""Governed DeepSeek adapter for teacher-only aggregate interpretation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from course_insight.contracts.analytics import TeacherAnalyticsBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMGenerationResult,
    LLMModelRef,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m9_teacher_analytics.policy import (
    DEFAULT_M9_NARRATIVE_POLICY,
    M9NarrativePolicy,
)
from course_insight.modules.m9_teacher_analytics.prompts import (
    NARRATIVE_PROMPT_ID,
    NARRATIVE_PROMPT_VERSION,
    NARRATIVE_SCHEMA_VERSION,
    REVIEW_QUESTION_CODES,
    NarrativePromptEnvelope,
    teacher_narrative_prompt,
)


@dataclass(frozen=True, slots=True)
class M9NarrativeOutcome:
    """Validated interpretation plus privacy-safe audit artifacts."""

    generation: LLMGenerationResult
    audit: ModelInvocationAudit
    safety: SafetyCheckResult
    prompt_record: dict[str, Any]


class M9InvocationFailure(Exception):
    """Carry safe failure artifacts across the service boundary."""

    def __init__(
        self,
        *,
        error: DomainError,
        generation: LLMGenerationResult,
        audit: ModelInvocationAudit,
        safety: SafetyCheckResult,
        prompt_record: dict[str, Any],
    ) -> None:
        super().__init__(str(error))
        self.error = error
        self.generation = generation
        self.audit = audit
        self.safety = safety
        self.prompt_record = prompt_record


@runtime_checkable
class GovernedM9NarrativeAdapter(Protocol):
    """Generate one validated interpretation from an existing report."""

    def narrate(
        self,
        bundle: TeacherAnalyticsBundle,
    ) -> M9NarrativeOutcome:
        """Return one aggregate-only, evidence-bound interpretation."""


class DeepSeekM9NarrativeAdapter:
    """Use DeepSeek without exposing learner identities or source prose."""

    def __init__(
        self,
        client: DeepSeekClient,
        policy: M9NarrativePolicy = DEFAULT_M9_NARRATIVE_POLICY,
    ) -> None:
        expected = (
            policy.model_name,
            policy.model_version,
            policy.thinking_enabled,
            float(policy.temperature),
            policy.max_attempts,
            policy.max_tokens,
        )
        actual = (
            client.model_name,
            client.model_version,
            client.policy.thinking_enabled,
            float(client.policy.temperature),
            client.policy.max_attempts,
            client.policy.max_tokens,
        )
        if actual != expected:
            raise ValueError(
                "DeepSeek client settings do not match the frozen M9 policy"
            )
        self._client = client
        self._policy = policy

    def narrate(
        self,
        bundle: TeacherAnalyticsBundle,
    ) -> M9NarrativeOutcome:
        """Generate, validate, and locally rehydrate one interpretation."""

        prompt = teacher_narrative_prompt(bundle, self._policy)
        request = LLMGenerationRequest(
            request_id=f"m9_interpretation_{uuid4().hex}",
            use_case="teacher_narrative",
            model_ref=LLMModelRef(
                provider="deepseek",
                model_name=self._client.model_name,
                model_version=self._client.model_version,
                api_key_env="DEEPSEEK_API_KEY",
                status="configured",
            ),
            prompt_template_id=NARRATIVE_PROMPT_ID,
            prompt_template_version=NARRATIVE_PROMPT_VERSION,
            evidence_ids=list(prompt.evidence_ids),
            input_checksum=prompt.input_checksum,
            created_at=bundle.generated_at,
        )
        invocation = self._client.invoke_json(
            request=request,
            messages=prompt.messages,
        )
        prompt_record = prompt.safe_record(
            request_id=request.request_id,
            policy_version=self._policy.policy_version,
        )
        self._require_success(
            request=request,
            generation=invocation.result,
            audit=invocation.audit,
            prompt_record=prompt_record,
        )
        try:
            generation = _validated_generation(
                invocation.result,
                prompt=prompt,
                policy=self._policy,
            )
        except (DomainError, TypeError, ValueError):
            self._invalid_output(
                request=request,
                generation=invocation.result,
                audit=invocation.audit,
                prompt_record=prompt_record,
            )
        return M9NarrativeOutcome(
            generation=generation,
            audit=invocation.audit,
            safety=SafetyCheckResult(
                request_id=request.request_id,
                status="passed",
                flags=[],
                checked_at=generation.generated_at,
            ),
            prompt_record=prompt_record,
        )

    @staticmethod
    def _require_success(
        *,
        request: LLMGenerationRequest,
        generation: LLMGenerationResult,
        audit: ModelInvocationAudit,
        prompt_record: dict[str, Any],
    ) -> None:
        if generation.status == "succeeded":
            return
        blocked = generation.status == "blocked"
        error_code = audit.error_code or "DEEPSEEK_INVOCATION_FAILED"
        if error_code in {
            "DEEPSEEK_API_KEY_MISSING",
            "DEEPSEEK_MODEL_UNCONFIGURED",
            "DEEPSEEK_MODEL_MISMATCH",
        }:
            domain_code = "MODEL_ADAPTER_UNCONFIGURED"
            message = "the governed DeepSeek interpretation adapter is not configured"
        elif blocked:
            domain_code = "MODEL_OUTPUT_BLOCKED"
            message = "DeepSeek blocked the governed teacher interpretation"
        else:
            domain_code = "MODEL_API_UNAVAILABLE"
            message = "DeepSeek teacher interpretation is temporarily unavailable"
        raise M9InvocationFailure(
            error=DomainError(
                code=domain_code,
                module="m9",
                message=message,
                details={
                    "request_id": request.request_id,
                    "provider_error_code": error_code,
                },
                recoverable=True,
            ),
            generation=generation,
            audit=audit,
            safety=SafetyCheckResult(
                request_id=request.request_id,
                status=("blocked" if blocked else "not_run"),
                flags=(["provider_content_filter"] if blocked else []),
                checked_at=generation.generated_at,
            ),
            prompt_record=prompt_record,
        )

    @staticmethod
    def _invalid_output(
        *,
        request: LLMGenerationRequest,
        generation: LLMGenerationResult,
        audit: ModelInvocationAudit,
        prompt_record: dict[str, Any],
    ) -> None:
        sanitized = LLMGenerationResult(
            request_id=generation.request_id,
            provider="deepseek",
            status="blocked",
            content="",
            structured_output={},
            citation_ids=[],
            finish_reason="blocked",
            generated_at=generation.generated_at,
        )
        raise M9InvocationFailure(
            error=DomainError(
                code="INVALID_MODEL_JSON",
                module="m9",
                message="DeepSeek returned output that violates the M9 contract",
                details={"request_id": request.request_id},
                recoverable=True,
            ),
            generation=sanitized,
            audit=audit,
            safety=SafetyCheckResult(
                request_id=request.request_id,
                status="blocked",
                flags=["invalid_model_output"],
                checked_at=generation.generated_at,
            ),
            prompt_record=prompt_record,
        )


def _validated_generation(
    generation: LLMGenerationResult,
    *,
    prompt: NarrativePromptEnvelope,
    policy: M9NarrativePolicy,
) -> LLMGenerationResult:
    data = generation.structured_output
    _require_exact_keys(
        data,
        {
            "schema_version",
            "source_digest",
            "fact_interpretations",
            "review_questions",
            "suggestion_explanations",
            "citation_ids",
        },
    )
    if (
        _text(data["schema_version"]) != NARRATIVE_SCHEMA_VERSION
        or _text(data["source_digest"]) != prompt.source_digest
    ):
        raise ValueError

    fact_interpretations = _fact_interpretations(
        data["fact_interpretations"],
        prompt=prompt,
        policy=policy,
    )
    review_questions = _review_questions(
        data["review_questions"],
        prompt=prompt,
        policy=policy,
    )
    suggestion_explanations = _suggestion_explanations(
        data["suggestion_explanations"],
        prompt=prompt,
        policy=policy,
    )
    expected_citations = [fact.fact_ref for fact in prompt.facts]
    citation_refs = _text_list(data["citation_ids"])
    if (
        citation_refs != expected_citations
        or generation.citation_ids != expected_citations
    ):
        raise ValueError

    structured = {
        "schema_version": NARRATIVE_SCHEMA_VERSION,
        "scope": "class_aggregate",
        "ai_notice": (
            "AI 辅助解读可能出错，仅供教师结合程序原报告核查，"
            "不构成评分、正式评价或教学决定。"
        ),
        "source_report_id": prompt.report_id,
        "source_report_checksum": prompt.report_checksum,
        "fact_interpretations": fact_interpretations,
        "review_questions": review_questions,
        "suggestion_explanations": suggestion_explanations,
        "omitted_suggestion_count": prompt.omitted_suggestion_count,
        "limitations": [
            "只解读达到首版样本门槛的匿名班级聚合事实。",
            "未向模型提供个体报告、复核队列、分数、原始作答或自由文本。",
            "模型只返回封闭代码与临时证据别名，展示文字由本地模板生成。",
        ],
    }
    content = json.dumps(
        structured,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return LLMGenerationResult(
        request_id=generation.request_id,
        provider="deepseek",
        status="succeeded",
        content=content,
        structured_output=structured,
        citation_ids=list(prompt.evidence_ids),
        finish_reason="stop",
        generated_at=generation.generated_at,
    )


def _fact_interpretations(
    value: Any,
    *,
    prompt: NarrativePromptEnvelope,
    policy: M9NarrativePolicy,
) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) != len(prompt.facts):
        raise ValueError
    result: list[dict[str, Any]] = []
    for raw, fact in zip(value, prompt.facts, strict=True):
        _require_exact_keys(
            raw,
            {"fact_ref", "meaning_code", "render_code"},
        )
        if (
            _text(raw["fact_ref"]) != fact.fact_ref
            or _text(raw["meaning_code"]) != fact.meaning_code
            or _text(raw["render_code"]) != fact.meaning_code
            or fact.meaning_code not in _FACT_TEMPLATES
        ):
            raise ValueError
        result.append(
            {
                "fact_ref": fact.fact_ref,
                "meaning_code": fact.meaning_code,
                "meaning_label": _MEANING_LABELS[fact.meaning_code],
                "text": _FACT_TEMPLATES[fact.meaning_code],
                "source_ids": list(fact.source_ids),
            }
        )
    return result


def _review_questions(
    value: Any,
    *,
    prompt: NarrativePromptEnvelope,
    policy: M9NarrativePolicy,
) -> list[dict[str, Any]]:
    if (
        type(value) is not list
        or not 1 <= len(value) <= policy.max_review_questions
    ):
        raise ValueError
    fact_by_ref = {fact.fact_ref: fact for fact in prompt.facts}
    binding_by_code = {
        binding.question_code: binding.fact_refs
        for binding in prompt.review_question_bindings
    }
    seen: set[tuple[str, tuple[str, ...]]] = set()
    result: list[dict[str, Any]] = []
    for raw in value:
        _require_exact_keys(raw, {"question_code", "fact_refs"})
        question_code = _text(raw["question_code"])
        if (
            question_code not in REVIEW_QUESTION_CODES
            or question_code not in _QUESTION_TEMPLATES
        ):
            raise ValueError
        fact_refs = _text_list(raw["fact_refs"], require_nonempty=True)
        if any(ref not in fact_by_ref for ref in fact_refs):
            raise ValueError
        expected_fact_refs = binding_by_code.get(question_code)
        if expected_fact_refs is None or tuple(fact_refs) != expected_fact_refs:
            raise ValueError
        identity = (question_code, tuple(fact_refs))
        if identity in seen:
            raise ValueError
        seen.add(identity)
        result.append(
            {
                "question_code": question_code,
                "text": _QUESTION_TEMPLATES[question_code],
                "fact_refs": fact_refs,
                "source_ids": list(
                    dict.fromkeys(
                        source_id
                        for ref in fact_refs
                        for source_id in fact_by_ref[ref].source_ids
                    )
                ),
            }
        )
    return result


def _suggestion_explanations(
    value: Any,
    *,
    prompt: NarrativePromptEnvelope,
    policy: M9NarrativePolicy,
) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) != len(prompt.suggestions):
        raise ValueError
    fact_by_ref = {fact.fact_ref: fact for fact in prompt.facts}
    result: list[dict[str, Any]] = []
    for raw, suggestion in zip(value, prompt.suggestions, strict=True):
        _require_exact_keys(
            raw,
            {
                "suggestion_ref",
                "action_code",
                "status_code",
                "explanation_code",
                "fact_refs",
            },
        )
        fact_refs = _text_list(raw["fact_refs"], require_nonempty=True)
        if (
            _text(raw["suggestion_ref"]) != suggestion.suggestion_ref
            or _text(raw["action_code"]) != suggestion.action_code
            or _text(raw["status_code"]) != suggestion.status_code
            or _text(raw["explanation_code"])
            != suggestion.explanation_code
            or suggestion.explanation_code not in _SUGGESTION_TEMPLATES
            or tuple(fact_refs) != suggestion.fact_refs
        ):
            raise ValueError
        result.append(
            {
                "suggestion_id": suggestion.suggestion_id,
                "action_type": suggestion.action_code,
                "status": suggestion.status_code,
                "canonical_content": suggestion.canonical_content,
                "explanation_code": suggestion.explanation_code,
                "explanation": _SUGGESTION_TEMPLATES[
                    suggestion.explanation_code
                ],
                "fact_refs": fact_refs,
                "source_ids": list(
                    dict.fromkeys(
                        list(suggestion.evidence_ids)
                        + [
                            source_id
                            for ref in fact_refs
                            for source_id in fact_by_ref[ref].source_ids
                        ]
                    )
                ),
            }
        )
    return result


def _require_exact_keys(value: Any, expected: set[str]) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError
    return value.strip()


def _text_list(
    value: Any,
    *,
    require_nonempty: bool = False,
) -> list[str]:
    if type(value) is not list:
        raise ValueError
    result = [_text(item) for item in value]
    if len(result) != len(set(result)) or (require_nonempty and not result):
        raise ValueError
    return result


_MEANING_LABELS = {
    "evidence_limited": "程序证据门槛尚未满足",
    "evidence_sufficient": "程序证据门槛已经满足",
    "concept_needs_attention": "该聚合概念事实值得教师关注",
    "concept_mixed": "该聚合概念事实呈现混合状态",
    "concept_stable": "该聚合概念事实相对稳定",
    "pattern_high": "该聚合错误模式出现较集中",
    "pattern_medium": "该聚合错误模式处于中间范围",
    "pattern_limited": "该聚合错误模式出现较有限",
}
_FACT_TEMPLATES = {
    "evidence_limited": (
        "程序证据门槛尚未满足，教师需结合原报告谨慎核查。"
    ),
    "evidence_sufficient": (
        "程序证据门槛已经满足，但仍需教师结合原报告判断。"
    ),
    "concept_needs_attention": (
        "该聚合概念事实值得教师关注，不能据此推断个体情况。"
    ),
    "concept_mixed": (
        "该聚合概念事实呈现混合状态，需结合课堂证据继续核查。"
    ),
    "concept_stable": (
        "该聚合概念事实相对稳定，仍不代表所有个体情况一致。"
    ),
    "pattern_high": (
        "该聚合错误模式出现较集中，需由教师结合原报告核查。"
    ),
    "pattern_medium": (
        "该聚合错误模式处于中间范围，需结合课堂情境核查。"
    ),
    "pattern_limited": (
        "该聚合错误模式出现较有限，当前不宜作扩大解释。"
    ),
}
_QUESTION_TEMPLATES = {
    "check_recent_classroom_evidence": (
        "这些程序事实与近期课堂活动中的证据是否一致？"
    ),
    "check_assessment_coverage": (
        "当前测评覆盖是否足以支持教师作进一步判断？"
    ),
    "check_concept_transfer": (
        "相关概念状态在不同课堂任务中是否呈现一致现象？"
    ),
    "check_misconception_context": (
        "相关错误模式是否集中出现在特定课堂情境中？"
    ),
}
_SUGGESTION_TEMPLATES = {
    "collect_rule_basis": (
        "该候选建议由确定性规则依据所列聚合事实触发，目的在于补充证据；"
        "是否采用由教师决定。"
    ),
    "targeted_support_rule_basis": (
        "该候选建议由确定性规则依据所列聚合事实触发，指向针对性支持；"
        "是否采用由教师决定。"
    ),
    "observe_rule_basis": (
        "该观察建议由确定性规则依据所列聚合事实触发；"
        "是否继续观察由教师决定。"
    ),
}


__all__ = [
    "DeepSeekM9NarrativeAdapter",
    "GovernedM9NarrativeAdapter",
    "M9InvocationFailure",
    "M9NarrativeOutcome",
]
