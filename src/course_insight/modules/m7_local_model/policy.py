"""Frozen, versioned execution policy for governed M7 scoring."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class M7ExecutionPolicy:
    """Keep model, prompt, review, and output limits auditable as one policy."""

    policy_version: str = "m7-governed-v1"
    model_name: str = "deepseek-v4-flash"
    model_version: str = "runtime-api"
    thinking_enabled: bool = False
    temperature: float = 0.0
    max_tokens: int = 4096
    max_student_answer_characters: int = 24_000
    max_evidence_characters: int = 64_000
    max_scoring_reason_characters: int = 600
    require_teacher_review_for_all_scores: bool = True

    def __post_init__(self) -> None:
        for field_name in ("policy_version", "model_name", "model_version"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.model_name not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise ValueError("M7 policy requires a supported DeepSeek model")
        if self.thinking_enabled:
            raise ValueError("M7 governed v1 requires non-thinking generation")
        if (
            type(self.temperature) not in {int, float}
            or not math.isfinite(self.temperature)
            or self.temperature != 0.0
        ):
            raise ValueError("M7 governed v1 requires temperature=0")
        limits = (
            self.max_tokens,
            self.max_student_answer_characters,
            self.max_evidence_characters,
            self.max_scoring_reason_characters,
        )
        if any(type(limit) is not int or limit <= 0 for limit in limits):
            raise ValueError("M7 execution limits must be positive integers")
        if not self.require_teacher_review_for_all_scores:
            raise ValueError("M7 governed v1 requires teacher review for every score")


DEFAULT_M7_EXECUTION_POLICY = M7ExecutionPolicy()


__all__ = ["DEFAULT_M7_EXECUTION_POLICY", "M7ExecutionPolicy"]
