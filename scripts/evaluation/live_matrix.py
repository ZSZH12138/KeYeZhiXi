"""Budget-bounded orchestration for explicit DeepSeek M7 live evaluations."""

from __future__ import annotations

import inspect
import math
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from course_insight.infrastructure.deepseek import DEEPSEEK_API_KEY_ENV
from course_insight.modules.m7_local_model.policy import DEEPSEEK_MODEL_CANDIDATES

from ._common import EvaluationInputError


@dataclass(frozen=True, slots=True)
class LiveEvaluationCase:
    case_id: str


@dataclass(frozen=True, slots=True)
class LiveCaseResult:
    case_id: str
    candidate_id: str
    provider_model: str
    system_fingerprint: str
    estimated_cost_usd: float
    status: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


LiveInvoker = Callable[[LiveEvaluationCase, str, bool], LiveCaseResult]
CostEstimator = Callable[[LiveEvaluationCase, str, bool], float]


def run_live_matrix(
    cases: Iterable[LiveEvaluationCase],
    *,
    invoke: LiveInvoker,
    estimate_cost: CostEstimator,
    live: bool,
    acknowledge_third_party_processing: bool,
    max_cases: int,
    budget_usd: float,
) -> dict[str, object]:
    """Run all four candidates with explicit consent and a pre-call budget gate.

    The injected invoker must call the governed M7 public adapter.  This
    orchestrator never accepts an API key and persists no answer, prompt, or
    provider response body.
    """

    if not live or not acknowledge_third_party_processing:
        raise EvaluationInputError("live evaluation requires both explicit acknowledgements")
    if type(max_cases) is not int or max_cases <= 0:
        raise EvaluationInputError("max_cases must be a positive integer")
    if (
        isinstance(budget_usd, bool)
        or not isinstance(budget_usd, (int, float))
        or not math.isfinite(float(budget_usd))
        or budget_usd <= 0
    ):
        raise EvaluationInputError("budget_usd must be positive")
    if "api_key" in inspect.signature(invoke).parameters:
        raise EvaluationInputError("live invokers must not accept raw API keys")
    if not os.environ.get(DEEPSEEK_API_KEY_ENV):
        raise EvaluationInputError("DEEPSEEK_API_KEY is not configured")
    selected = tuple(cases)[:max_cases]
    if not selected:
        raise EvaluationInputError("live evaluation requires at least one case")

    spent = 0.0
    results: list[dict[str, object]] = []
    for case in selected:
        if not isinstance(case, LiveEvaluationCase) or not case.case_id:
            raise EvaluationInputError("live evaluation case is invalid")
        for candidate in DEEPSEEK_MODEL_CANDIDATES.values():
            reserved = estimate_cost(
                case, candidate.model_name, candidate.thinking_enabled
            )
            if (
                isinstance(reserved, bool)
                or not isinstance(reserved, (int, float))
                or not math.isfinite(float(reserved))
                or reserved < 0.0
            ):
                raise EvaluationInputError("live cost estimate is invalid")
            if spent + float(reserved) > float(budget_usd):
                return _summary(results, spent=spent, stopped="pre_call_budget_guard")
            result = invoke(case, candidate.model_name, candidate.thinking_enabled)
            if not isinstance(result, LiveCaseResult):
                raise EvaluationInputError("live invoker returned an invalid result")
            if result.case_id != case.case_id or result.candidate_id != candidate.candidate_id:
                raise EvaluationInputError("live result identity mismatch")
            if (
                type(result.provider_model) is not str
                or not result.provider_model.strip()
                or len(result.provider_model) > 128
                or type(result.system_fingerprint) is not str
                or not result.system_fingerprint.strip()
                or len(result.system_fingerprint) > 256
            ):
                raise EvaluationInputError("live provider identity is invalid")
            numeric_result_fields = (
                result.estimated_cost_usd,
                result.latency_ms,
                result.input_tokens,
                result.output_tokens,
            )
            if (
                type(result.status) is not str
                or not result.status.strip()
                or type(result.latency_ms) is not int
                or type(result.input_tokens) is not int
                or type(result.output_tokens) is not int
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value < 0
                    for value in numeric_result_fields
                )
                or result.estimated_cost_usd > float(reserved)
            ):
                raise EvaluationInputError("live result exceeded its reserved cost")
            spent += result.estimated_cost_usd
            results.append(
                {
                    "case_id": result.case_id,
                    "candidate_id": result.candidate_id,
                    "provider_model": result.provider_model,
                    "system_fingerprint": result.system_fingerprint,
                    "status": result.status,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "estimated_cost_usd": result.estimated_cost_usd,
                }
            )
    return _summary(results, spent=spent, stopped="completed")


def _summary(
    results: list[dict[str, object]],
    *,
    spent: float,
    stopped: str,
) -> dict[str, object]:
    return {
        "schema_version": "m7-live-matrix-summary-v1",
        "result_count": len(results),
        "estimated_cost_usd": spent,
        "stopped_reason": stopped,
        "results": results,
        "contains_prompt_or_answer_text": False,
    }


__all__ = [
    "LiveCaseResult",
    "CostEstimator",
    "LiveEvaluationCase",
    "LiveInvoker",
    "run_live_matrix",
]
