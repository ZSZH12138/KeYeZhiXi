"""M7 persistence boundary for feedback and privacy-minimized call audits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from course_insight.contracts.assessment import RubricScoringResult
from course_insight.contracts.intelligence import (
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.tutoring import StudentFeedbackPackage


_LOWER_HEX = frozenset("0123456789abcdef")
_M7_AUDIT_SAFETY_FLAGS = frozenset(
    {
        "teacher_review_required",
        "outbound_privacy_allowed",
        "outbound_privacy_redacted",
        "outbound_privacy_blocked",
        "pii_email_redacted",
        "pii_student_number_redacted",
        "pii_prc_id_redacted",
        "pii_phone_redacted",
        "high_risk_name",
        "high_risk_address",
        "high_risk_health",
        "high_risk_family",
        "privacy_sensitive_detected",
        "privacy_other_detected",
        "privacy_reviewer_uncertain",
        "privacy_reviewer_unavailable",
        "privacy_reviewer_failed",
        "privacy_identifier_obfuscated",
        "redaction_meaning_loss",
        "provider_content_filter",
        "invalid_model_output",
        "low_confidence",
        "review_selector_fallback",
        "review_selector_hard_defer",
        "review_selector_insufficient_data",
        "review_selector_ood",
        "review_selector_risk_limit",
        "review_selector_shadow",
        "review_selector_all_review",
        "review_selector_deferred",
        "review_selector_candidate_accepted",
        "review_selector_selective",
        "review_selector_accepted",
    }
)
_M7_AUDIT_ERROR_CODES = frozenset(
    {
        "OUTBOUND_PRIVACY_BLOCKED",
        "INVALID_MODEL_OUTPUT",
        "DEEPSEEK_API_KEY_MISSING",
        "DEEPSEEK_MODEL_UNCONFIGURED",
        "DEEPSEEK_MODEL_MISMATCH",
        "DEEPSEEK_NETWORK_ERROR",
        "DEEPSEEK_AUTH_FAILED",
        "DEEPSEEK_ACCESS_DENIED",
        "DEEPSEEK_RATE_LIMITED",
        "DEEPSEEK_REQUEST_REJECTED",
        "DEEPSEEK_SERVICE_UNAVAILABLE",
        "DEEPSEEK_CONTENT_FILTERED",
        "DEEPSEEK_INCOMPLETE_RESPONSE",
        "DEEPSEEK_RESPONSE_INVALID",
        "DEEPSEEK_INVOCATION_FAILED",
    }
)


@dataclass(frozen=True, slots=True)
class M7ModelAuditRecord:
    """One durable call record with no prompt, answer, or provider response."""

    invocation_id: str
    request_id: str
    scoring_task_id: str
    provider: Literal["deepseek"]
    model_name: str
    model_version: str
    provider_status: Literal["not_run", "succeeded", "failed", "blocked"]
    validation_status: Literal["not_run", "passed", "blocked"]
    prompt_template_id: str
    prompt_template_version: str
    execution_policy_version: str
    privacy_policy_version: str
    privacy_decision: Literal["allowed", "redacted", "blocked"]
    student_answer_checksum: str
    outbound_answer_checksum: str
    prompt_input_checksum: str | None
    result_checksum: str | None
    evidence_ids: tuple[str, ...]
    safety_flags: tuple[str, ...]
    safety_checked_at: datetime
    redaction_count: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error_code: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        text_values = (
            self.invocation_id,
            self.request_id,
            self.scoring_task_id,
            self.model_name,
            self.model_version,
            self.prompt_template_id,
            self.prompt_template_version,
            self.execution_policy_version,
            self.privacy_policy_version,
        )
        if any(type(value) is not str or not value.strip() for value in text_values):
            raise ValueError("M7 model audit identity fields must not be blank")
        if (
            self.provider != "deepseek"
            or self.provider_status
            not in {"not_run", "succeeded", "failed", "blocked"}
            or self.validation_status not in {"not_run", "passed", "blocked"}
            or self.privacy_decision not in {"allowed", "redacted", "blocked"}
        ):
            raise ValueError("M7 model audit statuses are invalid")
        _validate_checksum(self.student_answer_checksum)
        _validate_checksum(self.outbound_answer_checksum)
        for checksum in (self.prompt_input_checksum, self.result_checksum):
            if checksum is not None:
                _validate_checksum(checksum)
        if (self.validation_status == "passed") != (
            self.result_checksum is not None
        ):
            raise ValueError("passed M7 audit requires a result checksum")
        if self.privacy_decision == "blocked":
            if (
                self.provider_status != "not_run"
                or self.validation_status != "blocked"
                or self.prompt_input_checksum is not None
            ):
                raise ValueError("privacy-blocked M7 calls must remain not-run")
        elif self.prompt_input_checksum is None:
            raise ValueError("provider-eligible M7 calls require a prompt checksum")
        if (
            type(self.safety_flags) is not tuple
            or len(self.safety_flags) != len(set(self.safety_flags))
            or not set(self.safety_flags) <= _M7_AUDIT_SAFETY_FLAGS
        ):
            raise ValueError("M7 model audit safety flags are invalid")
        if (
            type(self.evidence_ids) is not tuple
            or self.evidence_ids != tuple(sorted(set(self.evidence_ids)))
            or any(
                type(evidence_id) is not str or not evidence_id.strip()
                for evidence_id in self.evidence_ids
            )
        ):
            raise ValueError("M7 model audit evidence identities are invalid")
        counters = (
            self.redaction_count,
            self.input_tokens,
            self.output_tokens,
            self.latency_ms,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("M7 model audit counters must be nonnegative")
        if (
            self.error_code is not None
            and self.error_code not in _M7_AUDIT_ERROR_CODES
        ):
            raise ValueError("M7 model audit error code is invalid")
        if any(
            timestamp.tzinfo is None or timestamp.utcoffset() is None
            for timestamp in (self.created_at, self.safety_checked_at)
        ):
            raise ValueError("M7 model audit timestamps must be timezone-aware")

    @classmethod
    def from_artifacts(
        cls,
        *,
        prompt_record: dict[str, Any],
        invocation: ModelInvocationAudit,
        safety: SafetyCheckResult,
        result: RubricScoringResult | None,
    ) -> "M7ModelAuditRecord":
        """Consolidate call artifacts into one atomic persistence payload."""

        if (
            invocation.request_id != safety.request_id
            or prompt_record.get("request_id") != invocation.request_id
        ):
            raise ValueError("M7 model audit artifact identities must match")
        if safety.status == "passed":
            validation_status: Literal["not_run", "passed", "blocked"] = (
                "passed"
            )
        elif safety.status == "blocked":
            validation_status = "blocked"
        else:
            validation_status = "not_run"
        if validation_status == "passed" and result is None:
            raise ValueError("passed M7 model audit requires validated result")
        if validation_status != "passed" and result is not None:
            raise ValueError("blocked M7 model audit cannot retain a result")
        error_code = invocation.error_code
        if validation_status == "blocked" and error_code is None:
            error_code = "INVALID_MODEL_OUTPUT"
        prompt_checksum = prompt_record.get("input_checksum")
        if prompt_checksum is not None and type(prompt_checksum) is not str:
            raise ValueError("M7 prompt checksum is invalid")
        evidence_ids = prompt_record.get("evidence_ids")
        if type(evidence_ids) is not list or any(
            type(evidence_id) is not str for evidence_id in evidence_ids
        ):
            raise ValueError("M7 prompt evidence identities are invalid")
        return cls(
            invocation_id=invocation.invocation_id,
            request_id=invocation.request_id,
            scoring_task_id=_required_record_text(
                prompt_record,
                "scoring_task_id",
            ),
            provider=invocation.provider,
            model_name=invocation.model_name,
            model_version=_required_record_text(prompt_record, "model_version"),
            provider_status=invocation.status,
            validation_status=validation_status,
            prompt_template_id=_required_record_text(
                prompt_record,
                "prompt_template_id",
            ),
            prompt_template_version=_required_record_text(
                prompt_record,
                "prompt_template_version",
            ),
            execution_policy_version=_required_record_text(
                prompt_record,
                "execution_policy_version",
            ),
            privacy_policy_version=_required_record_text(
                prompt_record,
                "privacy_policy_version",
            ),
            privacy_decision=_required_record_text(
                prompt_record,
                "privacy_decision",
            ),  # type: ignore[arg-type]
            student_answer_checksum=_required_record_text(
                prompt_record,
                "student_answer_checksum",
            ),
            outbound_answer_checksum=_required_record_text(
                prompt_record,
                "outbound_answer_checksum",
            ),
            prompt_input_checksum=prompt_checksum,
            result_checksum=(
                None if result is None else result.content_checksum()
            ),
            evidence_ids=tuple(sorted(set(evidence_ids))),
            safety_flags=tuple(safety.flags),
            safety_checked_at=safety.checked_at,
            redaction_count=_required_record_int(
                prompt_record,
                "redaction_count",
            ),
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            latency_ms=invocation.latency_ms,
            error_code=error_code,
            created_at=invocation.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "request_id": self.request_id,
            "scoring_task_id": self.scoring_task_id,
            "provider": self.provider,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "provider_status": self.provider_status,
            "validation_status": self.validation_status,
            "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": self.prompt_template_version,
            "execution_policy_version": self.execution_policy_version,
            "privacy_policy_version": self.privacy_policy_version,
            "privacy_decision": self.privacy_decision,
            "student_answer_checksum": self.student_answer_checksum,
            "outbound_answer_checksum": self.outbound_answer_checksum,
            "prompt_input_checksum": self.prompt_input_checksum,
            "result_checksum": self.result_checksum,
            "evidence_ids": list(self.evidence_ids),
            "safety_flags": list(self.safety_flags),
            "safety_checked_at": self.safety_checked_at.isoformat(),
            "redaction_count": self.redaction_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "M7ModelAuditRecord":
        expected = {
            "invocation_id",
            "request_id",
            "scoring_task_id",
            "provider",
            "model_name",
            "model_version",
            "provider_status",
            "validation_status",
            "prompt_template_id",
            "prompt_template_version",
            "execution_policy_version",
            "privacy_policy_version",
            "privacy_decision",
            "student_answer_checksum",
            "outbound_answer_checksum",
            "prompt_input_checksum",
            "result_checksum",
            "evidence_ids",
            "safety_flags",
            "safety_checked_at",
            "redaction_count",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "error_code",
            "created_at",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ValueError("M7 model audit payload shape is invalid")
        flags = payload["safety_flags"]
        evidence_ids = payload["evidence_ids"]
        if type(flags) is not list or type(evidence_ids) is not list:
            raise ValueError("M7 model audit list fields are invalid")
        created_at = payload["created_at"]
        safety_checked_at = payload["safety_checked_at"]
        if type(created_at) is not str or type(safety_checked_at) is not str:
            raise ValueError("M7 model audit timestamps are invalid")
        return cls(
            **{
                **payload,
                "evidence_ids": tuple(evidence_ids),
                "safety_flags": tuple(flags),
                "safety_checked_at": datetime.fromisoformat(
                    safety_checked_at
                ),
                "created_at": datetime.fromisoformat(created_at),
            }
        )


@runtime_checkable
class M7Repository(Protocol):
    """Persistence operations owned exclusively by M7."""

    def save_execution_audit(self, record: M7ModelAuditRecord) -> None:
        """Atomically retain one privacy-minimized, idempotent call audit."""

    def get_execution_audit(
        self,
        invocation_id: str,
    ) -> M7ModelAuditRecord | None:
        """Load one audit for the service's administrator-only read path."""

    def purge_execution_audits_before(self, cutoff: datetime) -> int:
        """Delete audits older than the configured retention cutoff."""

    def save_prompt_record(
        self,
        prompt_id: str,
        prompt_payload: dict[str, Any],
    ) -> None:
        """Retain privacy-safe metadata for deterministic feedback only."""

    def save_feedback(self, package: StudentFeedbackPackage) -> None:
        """Retain one generated student-feedback package."""

    def insert_or_get_feedback(
        self,
        package: StudentFeedbackPackage,
    ) -> StudentFeedbackPackage:
        """Persist one feedback package or return its identical winner."""

    def get_feedback(
        self,
        feedback_id: str,
    ) -> StudentFeedbackPackage | None:
        """Load a generated package by stable feedback identity."""

    def get_feedback_for_task(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        """Load the package for one task and learner."""


def _required_record_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str or not value.strip():
        raise ValueError(f"M7 model audit field {key} is invalid")
    return value


def _required_record_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"M7 model audit field {key} is invalid")
    return value


def _validate_checksum(value: str) -> None:
    if type(value) is not str or len(value) != 64 or not set(value) <= _LOWER_HEX:
        raise ValueError("M7 model audit checksum is invalid")


__all__ = ["M7ModelAuditRecord", "M7Repository"]
