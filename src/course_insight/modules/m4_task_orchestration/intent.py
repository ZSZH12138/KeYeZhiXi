"""Private immutable values shared by the M4 intent adapter pipeline."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable


SUPPORTED_INTENT_LABELS = (
    "qa",
    "diagnostic",
    "practice",
    "correction",
    "stage_assessment",
)
_SUPPORTED_INTENT_LABELS = frozenset(SUPPORTED_INTENT_LABELS)
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*\Z")
_MAX_ADAPTER_TOKEN_LENGTH = 128
_MAX_REASON_CODE_LENGTH = 64
_MAX_REASON_CODES = 16
_REGISTERED_REASON_CODES = frozenset(
    {
        "adapter_abstained",
        "adapter_exception",
        "adapter_failed",
        "adapter_invalid",
        "adapter_out_of_scope",
        "adapter_unavailable",
        "below_min_confidence",
        "below_min_margin",
        "blank_student_text",
        "conflicting_high_precision_rules",
        "conflicting_legacy_rules",
        "malformed_prediction",
        "missing_adapter",
        "model_abstained",
        "model_accepted",
        "no_supported_intent",
        "out_of_scope_competitor",
        "outside_supported_scope",
        "shadow_accepted",
        "unsafe_adapter_metadata",
        "unsafe_prediction_metadata",
        "unsupported_hint",
        *(f"high_precision:{label}" for label in SUPPORTED_INTENT_LABELS),
    }
)


class IntentStatus(StrEnum):
    """Lifecycle status for a private intent prediction."""

    ACCEPTED = "accepted"
    ABSTAINED = "abstained"
    OUT_OF_SCOPE = "out_of_scope"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class IntentPrediction:
    """An immutable intent-adapter result with no source text attached."""

    label: str | None
    scores: Mapping[str, float]
    confidence: float | None
    margin: float | None
    status: IntentStatus
    adapter_id: str
    adapter_version: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        allows_missing_score_summary = self.status in {
            IntentStatus.UNAVAILABLE,
            IntentStatus.FAILED,
            IntentStatus.INVALID,
        }
        scores = _validated_scores(self.scores)
        object.__setattr__(self, "scores", MappingProxyType(scores))
        object.__setattr__(
            self,
            "reason_codes",
            _normalize_reason_codes(self.reason_codes),
        )
        if not isinstance(self.status, IntentStatus):
            raise ValueError("status must be an IntentStatus")
        if self.label is not None and self.label not in _SUPPORTED_INTENT_LABELS:
            raise ValueError("intent label must be supported")
        if self.status is IntentStatus.ACCEPTED and self.label is None:
            raise ValueError("accepted predictions require a label")
        if self.status is not IntentStatus.ACCEPTED and self.label is not None:
            raise ValueError("non-accepted predictions cannot have a label")
        if allows_missing_score_summary:
            if self.confidence is not None or self.margin is not None:
                raise ValueError(
                    "unavailable, failed, and invalid predictions cannot have summaries"
                )
        else:
            _validate_probability("confidence", self.confidence)
            _validate_probability("margin", self.margin)
        _validate_safe_token(
            "adapter_id",
            self.adapter_id,
            max_length=_MAX_ADAPTER_TOKEN_LENGTH,
        )
        _validate_safe_token(
            "adapter_version",
            self.adapter_version,
            max_length=_MAX_ADAPTER_TOKEN_LENGTH,
        )
        if self.status in {IntentStatus.ACCEPTED, IntentStatus.ABSTAINED}:
            top_label, confidence, margin = _score_summary(scores)
            if self.status is IntentStatus.ACCEPTED and self.label != top_label:
                raise ValueError("accepted label must match the top score")
            if not math.isclose(
                float(self.confidence),
                confidence,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("confidence must match the top score")
            if not math.isclose(
                float(self.margin),
                margin,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("margin must match the top-two score difference")

    @classmethod
    def accepted(
        cls,
        label: str,
        scores: Mapping[str, float],
        adapter_id: str,
        adapter_version: str,
        reason_codes: tuple[str, ...] = (),
    ) -> IntentPrediction:
        """Build an accepted candidate before policy thresholding."""

        validated_scores = _validated_scores(scores)
        _, confidence, margin = _score_summary(validated_scores)
        return cls(
            label=label,
            scores=validated_scores,
            confidence=confidence,
            margin=margin,
            status=IntentStatus.ACCEPTED,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            reason_codes=tuple(reason_codes),
        )

    @classmethod
    def out_of_scope(
        cls,
        scores: Mapping[str, float],
        confidence: float,
        margin: float,
        adapter_id: str,
        adapter_version: str,
        reason_codes: tuple[str, ...] = (),
    ) -> IntentPrediction:
        """Build a prediction that deliberately declines task classification."""

        return cls(
            label=None,
            scores=scores,
            confidence=confidence,
            margin=margin,
            status=IntentStatus.OUT_OF_SCOPE,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            reason_codes=tuple(reason_codes),
        )


@runtime_checkable
class IntentAdapter(Protocol):
    """Private adapter boundary; adapters never return persisted source text."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def predict(self, text: str) -> IntentPrediction: ...


def _validate_probability(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be a number between zero and one")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must be between zero and one")


def _normalize_reason_codes(reason_codes: object) -> tuple[str, ...]:
    if not isinstance(reason_codes, (list, tuple)):
        raise ValueError("reason codes must be a list or tuple of strings")
    normalized = tuple(reason_codes)
    if len(normalized) > _MAX_REASON_CODES:
        raise ValueError("too many reason codes")
    for code in normalized:
        _validate_safe_token(
            "reason code",
            code,
            max_length=_MAX_REASON_CODE_LENGTH,
        )
        if code not in _REGISTERED_REASON_CODES:
            raise ValueError("reason code must be a registered audit code")
    return normalized


def _validated_scores(
    value: object,
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("scores must be a mapping")
    if set(value) != _SUPPORTED_INTENT_LABELS:
        raise ValueError("scores must contain the exact supported intent labels")
    scores: dict[str, float] = {}
    for label in SUPPORTED_INTENT_LABELS:
        score = value[label]
        _validate_probability(f"score for {label}", score)
        scores[label] = float(score)
    return scores


def _score_summary(scores: Mapping[str, float]) -> tuple[str, float, float]:
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], SUPPORTED_INTENT_LABELS.index(item[0])),
    )
    top_label, confidence = ranked[0]
    margin = confidence - ranked[1][1]
    return top_label, confidence, margin


def _validate_safe_token(name: str, value: object, *, max_length: int) -> None:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _SAFE_TOKEN.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be a bounded safe token")
