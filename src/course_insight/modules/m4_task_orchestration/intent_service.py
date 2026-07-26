"""Persisted-first, privacy-preserving M4 intent orchestration."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import InitVar, dataclass, replace
from datetime import datetime, timezone
from typing import Literal

from course_insight.contracts.errors import DomainError
from course_insight.modules.m4_task_orchestration.intent import (
    IntentAdapter,
    IntentPrediction,
    IntentStatus,
)
from course_insight.modules.m4_task_orchestration.intent_identity import (
    build_intent_request_identity,
    input_checksum,
    normalize_hint,
)
from course_insight.modules.m4_task_orchestration.intent_policy import (
    IntentDecisionOutcome,
    IntentPolicy,
)
from course_insight.modules.m4_task_orchestration.intent_rules import (
    RuleMatch,
    match_high_precision_rules,
    match_legacy_rules,
)
from course_insight.modules.m4_task_orchestration.repository import M4Repository
from course_insight.modules.m4_task_orchestration.routing import (
    SUPPORTED_TASK_TYPES,
)


IntentMode = Literal["rules", "shadow", "active"]
RequestKeyFactory = Callable[[Mapping[str, str]], str]
_DECISION_SCHEMA_VERSION = 1
_DETERMINISTIC_ADAPTER_ID = "m4-deterministic-intent"
_DETERMINISTIC_ADAPTER_VERSION = "1"
@dataclass(frozen=True, slots=True)
class IntentRequest:
    """Private request value; it is never persisted as a whole."""
    student_text: str
    task_type_hint: str | None
    course_id: str
    class_id: str
    learner_id: str
    session_id: str
    knowledge_bundle_id: str
    course_package_id: str
@dataclass(frozen=True, slots=True)
class _ShadowObservation:
    label: str | None
    status: IntentStatus
    adapter_id: str
    adapter_version: str
    confidence: float | None
    margin: float | None
    reason_codes: tuple[str, ...]
    agrees: bool | None
@dataclass(frozen=True, slots=True)
class _DecisionOutcome:
    resolved_task_type: str | None
    decision_status: IntentStatus
    decision_source: str
    adapter_id: str
    adapter_version: str
    policy_version: str
    confidence: float | None = None
    margin: float | None = None
    reason_codes: tuple[str, ...] = ()
    shadow: _ShadowObservation | None = None
@dataclass(frozen=True, slots=True)
class StoredIntentDecision:
    """Frozen replay record containing metadata and checksums, never source text."""
    request_key: str
    resolved_task_type: str | None
    decision_status: IntentStatus
    decision_source: str
    adapter_id: str
    adapter_version: str
    policy_version: str
    confidence: float | None
    margin: float | None
    input_checksum: str
    reason_codes: tuple[str, ...]
    created_at: datetime
    shadow_label: str | None = None
    shadow_status: IntentStatus | None = None
    shadow_adapter_id: str | None = None
    shadow_adapter_version: str | None = None
    shadow_confidence: float | None = None
    shadow_margin: float | None = None
    shadow_reason_codes: tuple[str, ...] = ()
    shadow_agrees: bool | None = None
    schema_version: int = _DECISION_SCHEMA_VERSION
    payload_checksum: str | None = None
    _generate_checksum: InitVar[bool] = False

    def __post_init__(self, _generate_checksum: bool) -> None:
        object.__setattr__(self, "reason_codes", _validated_reason_codes(
            self.reason_codes
        ))
        object.__setattr__(self, "shadow_reason_codes", _validated_reason_codes(
            self.shadow_reason_codes
        ))
        self._validate_fields()
        calculated = self.recalculate_payload_checksum()
        if _generate_checksum and self.payload_checksum is None:
            object.__setattr__(self, "payload_checksum", calculated)
        elif (
            not isinstance(self.payload_checksum, str)
            or self.payload_checksum != calculated
        ):
            raise ValueError("intent decision payload checksum is invalid")
    @classmethod
    def from_outcome(
        cls,
        *,
        request_key: str,
        input_checksum: str,
        outcome: _DecisionOutcome,
        created_at: datetime,
    ) -> StoredIntentDecision:
        """Build a canonical replay row from an internal decision outcome."""
        shadow = outcome.shadow
        return cls(
            request_key=request_key,
            resolved_task_type=outcome.resolved_task_type,
            decision_status=outcome.decision_status,
            decision_source=outcome.decision_source,
            adapter_id=outcome.adapter_id,
            adapter_version=outcome.adapter_version,
            policy_version=outcome.policy_version,
            confidence=outcome.confidence,
            margin=outcome.margin,
            input_checksum=input_checksum,
            reason_codes=outcome.reason_codes,
            created_at=created_at,
            shadow_label=None if shadow is None else shadow.label,
            shadow_status=None if shadow is None else shadow.status,
            shadow_adapter_id=None if shadow is None else shadow.adapter_id,
            shadow_adapter_version=None if shadow is None else shadow.adapter_version,
            shadow_confidence=None if shadow is None else shadow.confidence,
            shadow_margin=None if shadow is None else shadow.margin,
            shadow_reason_codes=() if shadow is None else shadow.reason_codes,
            shadow_agrees=None if shadow is None else shadow.agrees,
            _generate_checksum=True,
        )
    def recalculate_payload_checksum(self) -> str:
        """Hash the canonical persisted payload, excluding the checksum itself."""
        serialized = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    def canonical_payload(self) -> dict[str, object]:
        """Return an isolated JSON-compatible checksum payload."""
        return {
            "request_key": self.request_key,
            "resolved_task_type": self.resolved_task_type,
            "decision_status": self.decision_status.value,
            "decision_source": self.decision_source,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "policy_version": self.policy_version,
            "confidence": self.confidence,
            "margin": self.margin,
            "input_checksum": self.input_checksum,
            "reason_codes": list(self.reason_codes),
            "shadow": self.shadow_payload(),
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
        }
    def shadow_payload(self) -> dict[str, object] | None:
        """Return an isolated JSON-compatible shadow observation."""
        if self.shadow_status is None:
            return None
        return {
            "label": self.shadow_label,
            "status": self.shadow_status.value,
            "adapter_id": self.shadow_adapter_id,
            "adapter_version": self.shadow_adapter_version,
            "confidence": self.shadow_confidence,
            "margin": self.shadow_margin,
            "reason_codes": list(self.shadow_reason_codes),
            "agrees": self.shadow_agrees,
        }
    def assert_integrity(self) -> None:
        """Revalidate an object returned by an untrusted persistence boundary."""
        self._validate_fields()
        if self.payload_checksum != self.recalculate_payload_checksum():
            raise ValueError("intent decision payload checksum is invalid")
    def _validate_fields(self) -> None:
        _validate_nonblank("request_key", self.request_key)
        _validate_status_and_label(
            self.decision_status, self.resolved_task_type, prefix="decision"
        )
        _validate_nonblank("decision_source", self.decision_source)
        _validate_nonblank("adapter_id", self.adapter_id)
        _validate_nonblank("adapter_version", self.adapter_version)
        _validate_nonblank("policy_version", self.policy_version)
        _validate_optional_probability("confidence", self.confidence)
        _validate_optional_probability("margin", self.margin)
        if self.decision_source == "active_model" and (
            self.confidence is None or self.margin is None
        ):
            raise ValueError("active model decisions require scores")
        _validate_sha256("input_checksum", self.input_checksum)
        _validate_created_at(self.created_at)
        if self.schema_version != _DECISION_SCHEMA_VERSION:
            raise ValueError("intent decision schema version is unsupported")
        self._validate_shadow()
    def _validate_shadow(self) -> None:
        shadow_fields = (
            self.shadow_label,
            self.shadow_adapter_id,
            self.shadow_adapter_version,
            self.shadow_confidence,
            self.shadow_margin,
            self.shadow_agrees,
        )
        if self.shadow_status is None:
            if any(value is not None for value in shadow_fields):
                raise ValueError("shadow fields require a shadow status")
            if self.shadow_reason_codes:
                raise ValueError("shadow reasons require a shadow status")
            return
        _validate_status_and_label(
            self.shadow_status, self.shadow_label, prefix="shadow"
        )
        _validate_nonblank("shadow_adapter_id", self.shadow_adapter_id)
        _validate_nonblank("shadow_adapter_version", self.shadow_adapter_version)
        _validate_optional_probability("shadow_confidence", self.shadow_confidence)
        _validate_optional_probability("shadow_margin", self.shadow_margin)
        if self.shadow_status is IntentStatus.ACCEPTED and (
            self.shadow_confidence is None or self.shadow_margin is None
        ):
            raise ValueError("accepted shadow observations require scores")
        if self.shadow_agrees is not None and not isinstance(
            self.shadow_agrees,
            bool,
        ):
            raise ValueError("shadow_agrees must be a boolean or None")
@dataclass(frozen=True, slots=True)
class _AdapterAttempt:
    label: str | None
    status: IntentStatus
    adapter_id: str
    adapter_version: str
    confidence: float | None
    margin: float | None
    reason_codes: tuple[str, ...]
class M4IntentService:
    """Resolve private learner intent with first-writer replay authority."""
    def __init__(
        self,
        repository: M4Repository,
        request_key_factory: RequestKeyFactory,
        *,
        mode: IntentMode = "rules",
        adapter: IntentAdapter | None = None,
        policy: IntentPolicy | None = None,
        policy_version: str = "m4-intent-policy-v1",
    ) -> None:
        if mode not in {"rules", "shadow", "active"}:
            raise ValueError("intent mode must be rules, shadow, or active")
        if not callable(request_key_factory):
            raise ValueError("request key factory must be callable")
        if mode in {"shadow", "active"} and adapter is None:
            raise ValueError(f"{mode} intent mode requires an adapter")
        _validate_nonblank("policy_version", policy_version)
        self._repository = repository
        self._request_key_factory = request_key_factory
        self._mode = mode
        self._adapter = adapter
        self._policy = policy or IntentPolicy(0.70, 0.10)
        self._policy_version = policy_version
    @property
    def repository(self) -> M4Repository:
        """Expose the shared persistence boundary for composition and inspection."""
        return self._repository
    def resolve(
        self,
        *,
        student_text: str,
        task_type_hint: str | None,
        course_id: str,
        class_id: str,
        learner_id: str,
        session_id: str,
        knowledge_bundle_id: str,
        course_package_id: str,
    ) -> str:
        """Return one supported type or a replayable recoverable refusal."""
        request = IntentRequest(
            student_text=student_text,
            task_type_hint=task_type_hint,
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            knowledge_bundle_id=knowledge_bundle_id,
            course_package_id=course_package_id,
        )
        normalized_text = _normalize_text(request.student_text)
        request_key = self._build_request_key(request, normalized_text)
        checksum = input_checksum(normalized_text)
        persisted = self._repository.get_intent_decision(request_key)
        if persisted is not None:
            return self._resolve_persisted(persisted, request_key, checksum)
        outcome = self._decide(request, normalized_text)
        candidate = StoredIntentDecision.from_outcome(
            request_key=request_key,
            input_checksum=checksum,
            outcome=outcome,
            created_at=self._created_at(),
        )
        winner = self._repository.insert_or_get_intent_decision(candidate)
        return self._resolve_persisted(winner, request_key, checksum)
    def _build_request_key(
        self,
        request: IntentRequest,
        normalized_text: str,
    ) -> str:
        try:
            identity = build_intent_request_identity(
                course_id=request.course_id,
                class_id=request.class_id,
                learner_id=request.learner_id,
                session_id=request.session_id,
                knowledge_bundle_id=request.knowledge_bundle_id,
                course_package_id=request.course_package_id,
                task_type_hint=request.task_type_hint,
                student_text=normalized_text,
            )
            request_key = self._request_key_factory(identity)
        except (TypeError, ValueError) as error:
            raise _unsupported("intent request identity is invalid") from error
        if not isinstance(request_key, str) or not request_key.strip():
            raise _unsupported("intent request key is invalid")
        return request_key
    def _decide(
        self,
        request: IntentRequest,
        normalized_text: str,
    ) -> _DecisionOutcome:
        if not normalized_text:
            return self._refusal(IntentStatus.INVALID, ("blank_student_text",))
        hint = normalize_hint(request.task_type_hint)
        if request.task_type_hint is not None:
            if hint not in SUPPORTED_TASK_TYPES:
                return self._refusal(IntentStatus.INVALID, ("unsupported_hint",))
            return self._deterministic_accept(
                hint,
                "legal_hint",
                normalized_text,
            )

        high_precision = match_high_precision_rules(normalized_text)
        if high_precision.resolved_label is not None:
            return self._deterministic_accept(
                high_precision.resolved_label,
                "high_precision_rule",
                normalized_text,
            )
        high_precision_conflict = len(high_precision.labels) > 1
        if self._mode == "active":
            return self._decide_active(
                normalized_text,
                high_precision,
                high_precision_conflict,
            )
        shadow = self._shadow_observation(normalized_text, None)
        return self._legacy_or_refusal(
            normalized_text,
            high_precision_conflict=high_precision_conflict,
            shadow=shadow,
        )
    def _deterministic_accept(
        self,
        label: str,
        source: str,
        normalized_text: str,
    ) -> _DecisionOutcome:
        shadow = self._shadow_observation(normalized_text, label)
        return _DecisionOutcome(
            resolved_task_type=label,
            decision_status=IntentStatus.ACCEPTED,
            decision_source=source,
            adapter_id=_DETERMINISTIC_ADAPTER_ID,
            adapter_version=_DETERMINISTIC_ADAPTER_VERSION,
            policy_version=self._policy_version,
            shadow=shadow,
        )
    def _decide_active(
        self,
        normalized_text: str,
        high_precision: RuleMatch,
        high_precision_conflict: bool,
    ) -> _DecisionOutcome:
        attempt = self._run_adapter(normalized_text)
        if attempt.status is IntentStatus.ACCEPTED:
            prediction = _prediction_from_attempt(attempt)
            policy_outcome = self._policy.accept(prediction)
            if policy_outcome.status is IntentStatus.ACCEPTED:
                return self._model_accept(attempt, policy_outcome.reason_codes)
            attempt = _attempt_with_policy(attempt, policy_outcome)
        if attempt.status is IntentStatus.OUT_OF_SCOPE:
            return self._model_refusal(attempt)
        return self._legacy_or_refusal(
            normalized_text,
            high_precision_conflict=high_precision_conflict,
            model_attempt=attempt,
            prior_rule_labels=high_precision.labels,
        )
    def _legacy_or_refusal(
        self,
        normalized_text: str,
        *,
        high_precision_conflict: bool,
        model_attempt: _AdapterAttempt | None = None,
        prior_rule_labels: tuple[str, ...] = (),
        shadow: _ShadowObservation | None = None,
    ) -> _DecisionOutcome:
        if high_precision_conflict:
            attempt_reasons = (
                () if model_attempt is None else model_attempt.reason_codes
            )
            reasons = _merge_reasons(
                attempt_reasons,
                ("conflicting_high_precision_rules",),
            )
            status = (
                IntentStatus.ABSTAINED
                if model_attempt is None
                else model_attempt.status
            )
            return self._refusal(
                status,
                reasons,
                model_attempt=model_attempt,
                shadow=shadow,
            )
        legacy = match_legacy_rules(normalized_text)
        if legacy.resolved_label is not None:
            return self._legacy_accept(
                legacy.resolved_label,
                model_attempt,
                shadow,
            )
        reason = (
            "conflicting_legacy_rules"
            if len(legacy.labels) > 1
            else "no_supported_intent"
        )
        attempt_reasons = () if model_attempt is None else model_attempt.reason_codes
        reasons = _merge_reasons(
            attempt_reasons,
            (reason,),
            tuple(f"high_precision:{label}" for label in prior_rule_labels),
        )
        status = (
            IntentStatus.ABSTAINED
            if model_attempt is None
            else model_attempt.status
        )
        return self._refusal(
            status,
            reasons,
            model_attempt=model_attempt,
            shadow=shadow,
        )
    def _legacy_accept(
        self,
        label: str,
        model_attempt: _AdapterAttempt | None,
        shadow: _ShadowObservation | None,
    ) -> _DecisionOutcome:
        adapter_id, adapter_version = _outcome_adapter_metadata(model_attempt)
        if shadow is not None and shadow.status is IntentStatus.ACCEPTED:
            shadow = replace(shadow, agrees=shadow.label == label)
        return _DecisionOutcome(
            resolved_task_type=label,
            decision_status=IntentStatus.ACCEPTED,
            decision_source="legacy_rule",
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            policy_version=self._policy_version,
            confidence=None if model_attempt is None else model_attempt.confidence,
            margin=None if model_attempt is None else model_attempt.margin,
            reason_codes=(
                () if model_attempt is None else model_attempt.reason_codes
            ),
            shadow=shadow,
        )
    def _model_accept(
        self,
        attempt: _AdapterAttempt,
        reason_codes: tuple[str, ...],
    ) -> _DecisionOutcome:
        return _DecisionOutcome(
            resolved_task_type=attempt.label,
            decision_status=IntentStatus.ACCEPTED,
            decision_source="active_model",
            adapter_id=attempt.adapter_id,
            adapter_version=attempt.adapter_version,
            policy_version=self._policy_version,
            confidence=attempt.confidence,
            margin=attempt.margin,
            reason_codes=reason_codes,
        )
    def _model_refusal(self, attempt: _AdapterAttempt) -> _DecisionOutcome:
        return self._refusal(
            attempt.status,
            attempt.reason_codes,
            model_attempt=attempt,
        )
    def _refusal(
        self,
        status: IntentStatus,
        reason_codes: tuple[str, ...],
        *,
        model_attempt: _AdapterAttempt | None = None,
        shadow: _ShadowObservation | None = None,
    ) -> _DecisionOutcome:
        adapter_id, adapter_version = _outcome_adapter_metadata(model_attempt)
        return _DecisionOutcome(
            resolved_task_type=None,
            decision_status=status,
            decision_source="refusal",
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            policy_version=self._policy_version,
            confidence=None if model_attempt is None else model_attempt.confidence,
            margin=None if model_attempt is None else model_attempt.margin,
            reason_codes=reason_codes,
            shadow=shadow,
        )
    def _shadow_observation(
        self,
        normalized_text: str,
        final_label: str | None,
    ) -> _ShadowObservation | None:
        if self._mode != "shadow":
            return None
        attempt = self._run_adapter(normalized_text)
        accepted = attempt.status is IntentStatus.ACCEPTED
        return _ShadowObservation(
            label=attempt.label,
            status=attempt.status,
            adapter_id=attempt.adapter_id,
            adapter_version=attempt.adapter_version,
            confidence=attempt.confidence,
            margin=attempt.margin,
            reason_codes=attempt.reason_codes,
            agrees=(
                attempt.label == final_label
                if accepted and final_label is not None
                else None
            ),
        )
    def _run_adapter(self, normalized_text: str) -> _AdapterAttempt:
        if self._adapter is None:
            return _invalid_attempt(
                "missing_adapter",
                _DETERMINISTIC_ADAPTER_ID,
                _DETERMINISTIC_ADAPTER_VERSION,
            )
        adapter_id, adapter_version = _safe_adapter_metadata(self._adapter)
        try:
            raw_prediction = self._adapter.predict(normalized_text)
        except Exception:
            return _AdapterAttempt(
                label=None,
                status=IntentStatus.ABSTAINED,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                confidence=None,
                margin=None,
                reason_codes=("adapter_exception",),
            )
        try:
            prediction = _validated_prediction(raw_prediction)
        except (AttributeError, TypeError, ValueError):
            return _invalid_attempt(
                "malformed_prediction",
                adapter_id,
                adapter_version,
            )
        return _AdapterAttempt(
            label=prediction.label,
            status=prediction.status,
            adapter_id=prediction.adapter_id,
            adapter_version=prediction.adapter_version,
            confidence=float(prediction.confidence),
            margin=float(prediction.margin),
            reason_codes=prediction.reason_codes,
        )
    def _resolve_persisted(
        self,
        persisted: StoredIntentDecision,
        request_key: str,
        expected_input_checksum: str,
    ) -> str:
        if not isinstance(persisted, StoredIntentDecision):
            raise RuntimeError("M4 persisted intent decision is corrupt")
        try:
            persisted.assert_integrity()
        except (TypeError, ValueError) as error:
            raise RuntimeError("M4 persisted intent decision is corrupt") from error
        if (
            persisted.request_key != request_key
            or persisted.input_checksum != expected_input_checksum
        ):
            raise RuntimeError(
                "M4 intent idempotency collision or persisted decision corruption"
            )
        if persisted.decision_status is IntentStatus.ACCEPTED:
            if persisted.resolved_task_type not in SUPPORTED_TASK_TYPES:
                raise RuntimeError("M4 persisted intent decision is corrupt")
            return persisted.resolved_task_type
        raise _unsupported(
            "student task could not be classified",
            reason_codes=list(persisted.reason_codes),
        )
    def _created_at(self) -> datetime:
        return datetime.now(timezone.utc)
def _normalize_text(student_text: str) -> str:
    if not isinstance(student_text, str):
        raise _unsupported("student task text must be a string")
    return " ".join(student_text.split()).casefold()
def _validated_prediction(raw_prediction: object) -> IntentPrediction:
    if not isinstance(raw_prediction, IntentPrediction):
        raise ValueError("adapter returned a non-prediction value")
    prediction = IntentPrediction(
        label=raw_prediction.label,
        confidence=raw_prediction.confidence,
        margin=raw_prediction.margin,
        status=raw_prediction.status,
        adapter_id=raw_prediction.adapter_id,
        adapter_version=raw_prediction.adapter_version,
        reason_codes=raw_prediction.reason_codes,
    )
    if prediction.status is not IntentStatus.ACCEPTED and prediction.label is not None:
        raise ValueError("non-accepted predictions cannot have a label")
    return prediction
def _prediction_from_attempt(attempt: _AdapterAttempt) -> IntentPrediction:
    if attempt.label is None or attempt.confidence is None or attempt.margin is None:
        raise ValueError("accepted adapter attempt is incomplete")
    return IntentPrediction.accepted(
        label=attempt.label,
        confidence=attempt.confidence,
        margin=attempt.margin,
        adapter_id=attempt.adapter_id,
        adapter_version=attempt.adapter_version,
        reason_codes=attempt.reason_codes,
    )
def _attempt_with_policy(
    attempt: _AdapterAttempt,
    policy_outcome: IntentDecisionOutcome,
) -> _AdapterAttempt:
    return _AdapterAttempt(
        label=None,
        status=policy_outcome.status,
        adapter_id=attempt.adapter_id,
        adapter_version=attempt.adapter_version,
        confidence=attempt.confidence,
        margin=attempt.margin,
        reason_codes=policy_outcome.reason_codes,
    )
def _safe_adapter_metadata(adapter: IntentAdapter) -> tuple[str, str]:
    try:
        adapter_id = adapter.adapter_id
    except Exception:
        adapter_id = None
    try:
        adapter_version = adapter.adapter_version
    except Exception:
        adapter_version = None
    return (
        adapter_id
        if isinstance(adapter_id, str) and adapter_id.strip()
        else "invalid-adapter",
        (
            adapter_version
            if isinstance(adapter_version, str) and adapter_version.strip()
            else "unavailable"
        ),
    )


def _invalid_attempt(
    reason_code: str,
    adapter_id: str,
    adapter_version: str,
) -> _AdapterAttempt:
    return _AdapterAttempt(
        label=None,
        status=IntentStatus.INVALID,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        confidence=None,
        margin=None,
        reason_codes=(reason_code,),
    )


def _outcome_adapter_metadata(
    attempt: _AdapterAttempt | None,
) -> tuple[str, str]:
    if attempt is None:
        return _DETERMINISTIC_ADAPTER_ID, _DETERMINISTIC_ADAPTER_VERSION
    return attempt.adapter_id, attempt.adapter_version


def _merge_reasons(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


def _validated_reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("reason codes must be a list or tuple")
    normalized = tuple(value)
    if any(not isinstance(code, str) or not code.strip() for code in normalized):
        raise ValueError("reason codes must contain nonblank strings")
    return normalized


def _validate_status_and_label(
    status: object,
    label: object,
    *,
    prefix: str,
) -> None:
    if not isinstance(status, IntentStatus):
        raise ValueError(f"{prefix} status is invalid")
    if label is not None and label not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"{prefix} task type is unsupported")
    if status is IntentStatus.ACCEPTED and label is None:
        raise ValueError(f"accepted {prefix} requires a task type")
    if status is not IntentStatus.ACCEPTED and label is not None:
        raise ValueError(f"non-accepted {prefix} cannot have a task type")


def _validate_optional_probability(name: str, value: object) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{name} must be a finite probability or None")


def _validate_nonblank(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _validate_sha256(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _validate_created_at(value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("created_at must be timezone-aware")


def _unsupported(message: str, **details: object) -> DomainError:
    return DomainError(
        code="UNSUPPORTED_TASK",
        module="m4",
        message=message,
        details=dict(details),
        recoverable=True,
    )
