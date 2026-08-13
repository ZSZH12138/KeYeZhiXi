"""Trust-boundary validation for replaceable M4 intent adapters."""

from __future__ import annotations

from course_insight.modules.m4_task_orchestration.intent import (
    IntentAdapter,
    IntentPrediction,
    _normalize_reason_codes,
    _validate_safe_token,
)


_INVALID_ADAPTER_ID = "invalid-adapter"
_INVALID_ADAPTER_VERSION = "unavailable"
_MAX_ADAPTER_TOKEN_LENGTH = 128


class PredictionBoundaryError(ValueError):
    """A fixed-code adapter boundary failure that never contains raw payloads."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def inspect_adapter_metadata(adapter: IntentAdapter) -> tuple[str, str, bool]:
    """Return bounded outer identity and whether it is safe to invoke."""

    try:
        adapter_id = adapter.adapter_id
        adapter_version = adapter.adapter_version
        _validate_safe_token(
            "adapter_id",
            adapter_id,
            max_length=_MAX_ADAPTER_TOKEN_LENGTH,
        )
        _validate_safe_token(
            "adapter_version",
            adapter_version,
            max_length=_MAX_ADAPTER_TOKEN_LENGTH,
        )
    except Exception:
        return _INVALID_ADAPTER_ID, _INVALID_ADAPTER_VERSION, False
    return adapter_id, adapter_version, True


def validate_prediction(
    raw_prediction: object,
    *,
    adapter_id: str,
    adapter_version: str,
) -> IntentPrediction:
    """Copy and validate a prediction using the outer adapter identity."""

    if not isinstance(raw_prediction, IntentPrediction):
        raise PredictionBoundaryError("malformed_prediction")
    try:
        if (
            raw_prediction.adapter_id != adapter_id
            or raw_prediction.adapter_version != adapter_version
        ):
            raise PredictionBoundaryError("unsafe_prediction_metadata")
        reason_codes = _normalize_reason_codes(raw_prediction.reason_codes)
    except PredictionBoundaryError:
        raise
    except Exception:
        raise PredictionBoundaryError("unsafe_prediction_metadata") from None
    try:
        prediction = IntentPrediction(
            label=raw_prediction.label,
            scores=raw_prediction.scores,
            confidence=raw_prediction.confidence,
            margin=raw_prediction.margin,
            status=raw_prediction.status,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            reason_codes=reason_codes,
        )
    except Exception:
        raise PredictionBoundaryError("malformed_prediction") from None
    return prediction
