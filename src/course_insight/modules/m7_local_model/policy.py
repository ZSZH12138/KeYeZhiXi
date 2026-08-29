"""Frozen, versioned execution policy for governed M7 scoring."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Literal, Mapping


DEEPSEEK_MODEL_CATALOG_VERSION = "deepseek-v4-api-2026-04-24"


@dataclass(frozen=True, slots=True)
class DeepSeekModelCandidate:
    """One audited DeepSeek V4 model/thinking-mode candidate."""

    candidate_id: str
    model_name: Literal["deepseek-v4-flash", "deepseek-v4-pro"]
    thinking_enabled: bool
    catalog_version: str = DEEPSEEK_MODEL_CATALOG_VERSION

    @property
    def thinking_mode(self) -> Literal["thinking", "non_thinking"]:
        return "thinking" if self.thinking_enabled else "non_thinking"


def _candidate(
    model_name: Literal["deepseek-v4-flash", "deepseek-v4-pro"],
    thinking_enabled: bool,
) -> DeepSeekModelCandidate:
    tier = model_name.removeprefix("deepseek-v4-")
    mode = "thinking" if thinking_enabled else "non-thinking"
    return DeepSeekModelCandidate(
        candidate_id=(
            f"{model_name}-{mode}@{DEEPSEEK_MODEL_CATALOG_VERSION}"
        ),
        model_name=model_name,
        thinking_enabled=thinking_enabled,
    )


_CANDIDATES = tuple(
    _candidate(model_name, thinking_enabled)
    for model_name in ("deepseek-v4-flash", "deepseek-v4-pro")
    for thinking_enabled in (False, True)
)
DEEPSEEK_MODEL_CANDIDATES: Mapping[
    tuple[str, bool], DeepSeekModelCandidate
] = MappingProxyType(
    {
        (candidate.model_name, candidate.thinking_enabled): candidate
        for candidate in _CANDIDATES
    }
)


@dataclass(frozen=True, slots=True)
class M7ExecutionPolicy:
    """Keep model, prompt, review, and output limits auditable as one policy."""

    policy_version: str = "m7-governed-v3"
    model_catalog_version: str = DEEPSEEK_MODEL_CATALOG_VERSION
    model_name: str = "deepseek-v4-flash"
    model_version: str = "runtime-api"
    thinking_enabled: bool = False
    temperature: float = 0.0
    max_tokens: int = 4096
    max_student_answer_characters: int = 24_000
    max_evidence_characters: int = 64_000
    max_scoring_reason_characters: int = 600
    format_retry_count: int = 8

    def __post_init__(self) -> None:
        for field_name in (
            "policy_version",
            "model_catalog_version",
            "model_name",
            "model_version",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.model_catalog_version != DEEPSEEK_MODEL_CATALOG_VERSION:
            raise ValueError("unsupported DeepSeek model catalog version")
        if type(self.thinking_enabled) is not bool:
            raise ValueError("M7 thinking mode must be boolean")
        if (self.model_name, self.thinking_enabled) not in (
            DEEPSEEK_MODEL_CANDIDATES
        ):
            raise ValueError("M7 policy requires a supported DeepSeek candidate")
        if (
            type(self.temperature) not in {int, float}
            or not math.isfinite(self.temperature)
            or self.temperature != 0.0
        ):
            raise ValueError("M7 governed scoring requires temperature=0")
        limits = (
            self.max_tokens,
            self.max_student_answer_characters,
            self.max_evidence_characters,
            self.max_scoring_reason_characters,
        )
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ValueError("M7 execution limits must be positive integers")
        if type(self.format_retry_count) is not int or self.format_retry_count != 8:
            raise ValueError("M7 scoring requires exactly eight format retries")

    @property
    def max_format_attempts(self) -> int:
        """Return the initial scoring call plus eight format retries."""

        return self.format_retry_count + 1

    @property
    def model_candidate(self) -> DeepSeekModelCandidate:
        """Return the immutable catalog entry selected by this policy."""

        return DEEPSEEK_MODEL_CANDIDATES[
            (self.model_name, self.thinking_enabled)
        ]

DEFAULT_M7_EXECUTION_POLICY = M7ExecutionPolicy()


__all__ = [
    "DEEPSEEK_MODEL_CANDIDATES",
    "DEEPSEEK_MODEL_CATALOG_VERSION",
    "DEFAULT_M7_EXECUTION_POLICY",
    "DeepSeekModelCandidate",
    "M7ExecutionPolicy",
]
