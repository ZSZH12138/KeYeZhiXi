"""Private immutable values shared by the M4 intent adapter pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


_SUPPORTED_INTENT_LABELS = frozenset(
    {"qa", "diagnostic", "practice", "correction", "stage_assessment"}
)


class IntentStatus(StrEnum):
    """Lifecycle status for a private intent prediction."""

    ACCEPTED = "accepted"
    ABSTAINED = "abstained"
    OUT_OF_SCOPE = "out_of_scope"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class IntentPrediction:
    """An immutable intent-adapter result with no source text attached."""

    label: str | None
    confidence: float
    margin: float
    status: IntentStatus
    adapter_id: str
    adapter_version: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, IntentStatus):
            raise ValueError("status must be an IntentStatus")
        if self.label is not None and self.label not in _SUPPORTED_INTENT_LABELS:
            raise ValueError("intent label must be supported")
        if self.status is IntentStatus.ACCEPTED and self.label is None:
            raise ValueError("accepted predictions require a label")
        if self.status is IntentStatus.OUT_OF_SCOPE and self.label is not None:
            raise ValueError("out-of-scope predictions cannot have a label")
        _validate_probability("confidence", self.confidence)
        _validate_probability("margin", self.margin)
        _validate_nonblank("adapter_id", self.adapter_id)
        _validate_nonblank("adapter_version", self.adapter_version)
        if any(
            not isinstance(code, str) or not code.strip()
            for code in self.reason_codes
        ):
            raise ValueError("reason codes must be nonblank strings")

    @classmethod
    def accepted(
        cls,
        label: str,
        confidence: float,
        margin: float,
        adapter_id: str,
        adapter_version: str,
        reason_codes: tuple[str, ...] = (),
    ) -> IntentPrediction:
        """Build an accepted candidate before policy thresholding."""

        return cls(
            label=label,
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
        confidence: float,
        margin: float,
        adapter_id: str,
        adapter_version: str,
        reason_codes: tuple[str, ...] = (),
    ) -> IntentPrediction:
        """Build a prediction that deliberately declines task classification."""

        return cls(
            label=None,
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


def _validate_probability(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number between zero and one")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must be between zero and one")


def _validate_nonblank(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
