"""Frozen execution policy for M9's teacher-only DeepSeek interpretation."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class M9NarrativePolicy:
    """Keep the first interpretation boundary conservative and auditable.

    This is an engineering boundary, not a claim that a group of this size is
    anonymous in every context.  The threshold is intentionally frozen for the
    initial release and must be recalibrated only after whole-system testing.
    """

    policy_version: str = "m9-teacher-interpretation-v2"
    model_name: str = "deepseek-v4-flash"
    model_version: str = "runtime-api"
    thinking_enabled: bool = False
    temperature: float = 0.0
    max_attempts: int = 1
    max_tokens: int = 1536
    max_prompt_characters: int = 48_000
    minimum_aggregate_size: int = 5
    max_facts: int = 50
    max_suggestions: int = 25
    max_review_questions: int = 3
    max_interpretation_characters: int = 240
    max_question_characters: int = 180
    require_aggregate_only_payload: bool = True

    def __post_init__(self) -> None:
        for field_name in ("policy_version", "model_name", "model_version"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.model_name not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise ValueError("M9 interpretation requires a supported DeepSeek model")
        if self.thinking_enabled:
            raise ValueError("M9 interpretation v1 requires non-thinking generation")
        if (
            type(self.temperature) not in {int, float}
            or not math.isfinite(self.temperature)
            or self.temperature != 0.0
        ):
            raise ValueError("M9 interpretation v1 requires temperature=0")
        if self.max_attempts != 1:
            raise ValueError("M9 interpretation v1 forbids automatic model retries")
        limits = (
            self.max_tokens,
            self.max_prompt_characters,
            self.minimum_aggregate_size,
            self.max_facts,
            self.max_suggestions,
            self.max_review_questions,
            self.max_interpretation_characters,
            self.max_question_characters,
        )
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ValueError("M9 interpretation limits must be positive integers")
        if not self.require_aggregate_only_payload:
            raise ValueError("M9 interpretation v1 requires aggregate-only model input")


DEFAULT_M9_NARRATIVE_POLICY = M9NarrativePolicy()


__all__ = ["DEFAULT_M9_NARRATIVE_POLICY", "M9NarrativePolicy"]
