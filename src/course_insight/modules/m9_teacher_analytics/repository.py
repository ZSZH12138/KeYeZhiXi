"""M9 repository boundary for analytics, reviews, and model-call audits."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Literal, Protocol

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.contracts.intelligence import (
    LLMGenerationResult,
    ModelInvocationAudit,
    SafetyCheckResult,
)


_REVIEW_TABLE = "m9_teacher_reviews"
_LOWER_HEX = frozenset("0123456789abcdef")


def analytics_learner_scope(
    bundle: TeacherAnalyticsBundle,
    learner_scope_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Validate the private learner index, including rejection tombstones.

    The public v1 contract cannot put a null score in ``IndividualReport``.
    A pending-rescore report therefore omits individual reports and keeps its
    affected learner only in M9-owned persistence metadata.  This private
    tombstone makes learner-scoped latest-report reads stop at the rejection
    instead of falling back to an older provisional score.
    """

    if not isinstance(bundle, TeacherAnalyticsBundle):
        raise TypeError("bundle must be a TeacherAnalyticsBundle")
    individual_ids = tuple(
        sorted(report.learner_id for report in bundle.individual_reports)
    )
    if learner_scope_ids is None:
        normalized = individual_ids
    else:
        if isinstance(learner_scope_ids, (str, bytes, bytearray)):
            raise TypeError("learner_scope_ids must be a sequence of identifiers")
        values = tuple(learner_scope_ids)
        if any(
            type(value) is not str
            or not value.strip()
            or value != value.strip()
            for value in values
        ):
            raise ValueError("M9 learner scope identifiers are invalid")
        normalized = tuple(sorted(values))
        if len(normalized) != len(set(normalized)):
            raise ValueError("M9 learner scope identifiers must be unique")

    pending_rescore = bundle.class_report.evidence_status == "pending_rescore"
    if pending_rescore:
        if (
            bundle.individual_reports
            or len(normalized) != 1
            or "_rejected_" not in bundle.report_id
        ):
            raise ValueError("M9 pending-rescore learner tombstone is invalid")
    elif normalized != individual_ids:
        raise ValueError("M9 learner scope does not match individual reports")
    return normalized


class M9ReviewDecisionConflict(RuntimeError):
    """Signal a competing decision for one immutable audit version."""

    def __init__(
        self,
        *,
        audit_id: str,
        expected_audit_version: int,
    ) -> None:
        super().__init__("M9 teacher-review decision conflict")
        self.audit_id = audit_id
        self.expected_audit_version = expected_audit_version


@dataclass(frozen=True, slots=True)
class M9ModelAuditRecord:
    """One M9-owned call record without raw prompt or provider response."""

    invocation_id: str
    request_id: str
    source_report_id: str
    source_report_checksum: str
    scope: Literal["class_aggregate"]
    prompt_template_id: str
    prompt_template_version: str
    output_schema_version: str
    policy_version: str
    input_checksum: str
    source_digest: str
    provider: Literal["deepseek"]
    model_name: str
    provider_status: Literal["not_run", "succeeded", "failed", "blocked"]
    validation_status: Literal["not_run", "passed", "blocked"]
    safety_flags: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error_code: str | None
    output_checksum: str | None
    validated_output: dict[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        text_values = (
            self.invocation_id,
            self.request_id,
            self.source_report_id,
            self.prompt_template_id,
            self.prompt_template_version,
            self.output_schema_version,
            self.policy_version,
            self.model_name,
        )
        if any(not value.strip() for value in text_values):
            raise ValueError("M9 model audit identity fields must not be blank")
        if (
            self.scope != "class_aggregate"
            or self.provider != "deepseek"
            or self.provider_status
            not in {"not_run", "succeeded", "failed", "blocked"}
            or self.validation_status not in {"not_run", "passed", "blocked"}
            or type(self.validated_output) is not dict
        ):
            raise ValueError("M9 model audit status or scope is invalid")
        if self.error_code is not None and (
            type(self.error_code) is not str or not self.error_code.strip()
        ):
            raise ValueError("M9 model audit error code is invalid")
        for checksum in (
            self.source_report_checksum,
            self.input_checksum,
            self.source_digest,
        ):
            _validate_checksum(checksum)
        if any(
            type(value) is not int or value < 0
            for value in (self.input_tokens, self.output_tokens, self.latency_ms)
        ):
            raise ValueError("M9 model audit counters must be nonnegative integers")
        if (
            type(self.safety_flags) is not tuple
            or len(self.safety_flags) != len(set(self.safety_flags))
            or any(
                type(flag) is not str or not flag.strip()
                for flag in self.safety_flags
            )
        ):
            raise ValueError("M9 model audit safety flags must be unique")
        passed = self.validation_status == "passed"
        if passed != bool(self.validated_output):
            raise ValueError("only a passed M9 audit may retain validated output")
        if passed != (self.output_checksum is not None):
            raise ValueError("passed M9 audit requires an output checksum")
        if self.output_checksum is not None:
            _validate_checksum(self.output_checksum)
            if self.output_checksum != _json_checksum(self.validated_output):
                raise ValueError("M9 model audit output checksum mismatch")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("M9 model audit timestamp must be timezone-aware")

    @classmethod
    def from_artifacts(
        cls,
        *,
        prompt_record: dict[str, Any],
        generation: LLMGenerationResult,
        invocation: ModelInvocationAudit,
        safety: SafetyCheckResult,
    ) -> "M9ModelAuditRecord":
        """Consolidate one call while retaining only validated teacher output."""

        if (
            invocation.request_id != generation.request_id
            or safety.request_id != generation.request_id
            or prompt_record.get("request_id") != generation.request_id
        ):
            raise ValueError("M9 model audit artifact identities must match")
        validation_status: Literal["not_run", "passed", "blocked"]
        if safety.status == "passed":
            validation_status = "passed"
        elif safety.status == "blocked":
            validation_status = "blocked"
        else:
            validation_status = "not_run"
        validated_output = (
            json.loads(
                json.dumps(
                    generation.structured_output,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
            if validation_status == "passed"
            else {}
        )
        error_code = invocation.error_code
        if validation_status == "blocked" and error_code is None:
            error_code = "INVALID_MODEL_OUTPUT"
        return cls(
            invocation_id=invocation.invocation_id,
            request_id=invocation.request_id,
            source_report_id=_required_record_text(
                prompt_record,
                "source_report_id",
            ),
            source_report_checksum=_required_record_text(
                prompt_record,
                "source_report_checksum",
            ),
            scope="class_aggregate",
            prompt_template_id=_required_record_text(
                prompt_record,
                "prompt_template_id",
            ),
            prompt_template_version=_required_record_text(
                prompt_record,
                "prompt_template_version",
            ),
            output_schema_version=_required_record_text(
                prompt_record,
                "output_schema_version",
            ),
            policy_version=_required_record_text(
                prompt_record,
                "policy_version",
            ),
            input_checksum=_required_record_text(
                prompt_record,
                "input_checksum",
            ),
            source_digest=_required_record_text(
                prompt_record,
                "source_digest",
            ),
            provider="deepseek",
            model_name=invocation.model_name,
            provider_status=invocation.status,
            validation_status=validation_status,
            safety_flags=tuple(safety.flags),
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            latency_ms=invocation.latency_ms,
            error_code=error_code,
            output_checksum=(
                _json_checksum(validated_output)
                if validation_status == "passed"
                else None
            ),
            validated_output=validated_output,
            created_at=invocation.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "request_id": self.request_id,
            "source_report_id": self.source_report_id,
            "source_report_checksum": self.source_report_checksum,
            "scope": self.scope,
            "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": self.prompt_template_version,
            "output_schema_version": self.output_schema_version,
            "policy_version": self.policy_version,
            "input_checksum": self.input_checksum,
            "source_digest": self.source_digest,
            "provider": self.provider,
            "model_name": self.model_name,
            "provider_status": self.provider_status,
            "validation_status": self.validation_status,
            "safety_flags": list(self.safety_flags),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "output_checksum": self.output_checksum,
            "validated_output": json.loads(
                json.dumps(
                    self.validated_output,
                    ensure_ascii=False,
                    allow_nan=False,
                )
            ),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "M9ModelAuditRecord":
        expected = {
            "invocation_id",
            "request_id",
            "source_report_id",
            "source_report_checksum",
            "scope",
            "prompt_template_id",
            "prompt_template_version",
            "output_schema_version",
            "policy_version",
            "input_checksum",
            "source_digest",
            "provider",
            "model_name",
            "provider_status",
            "validation_status",
            "safety_flags",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "error_code",
            "output_checksum",
            "validated_output",
            "created_at",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("M9 model audit payload fields are invalid")
        payload = dict(value)
        flags = payload["safety_flags"]
        output = payload["validated_output"]
        if type(flags) is not list or type(output) is not dict:
            raise ValueError("M9 model audit payload types are invalid")
        payload["safety_flags"] = tuple(flags)
        payload["created_at"] = datetime.fromisoformat(
            _required_record_text(payload, "created_at")
        )
        return cls(**payload)


class M9Repository(Protocol):
    """Persistence operations owned exclusively by M9."""

    def purge_actor(self, actor_id: str) -> int:
        """Physically delete module-owned rows for one actor."""

    def save_model_audit(self, record: M9ModelAuditRecord) -> None:
        """Persist one privacy-minimized M9 DeepSeek call record."""

    def get_model_audit(
        self,
        invocation_id: str,
    ) -> M9ModelAuditRecord | None:
        """Load one M9 DeepSeek call record by invocation identity."""

    def save_analytics(self, bundle: TeacherAnalyticsBundle) -> None:
        """Persist one generated teacher-analytics bundle."""

    def insert_or_get_analytics(
        self,
        bundle: TeacherAnalyticsBundle,
        *,
        course_id: str,
        learner_scope_ids: Sequence[str] | None = None,
    ) -> TeacherAnalyticsBundle:
        """Persist one scoped report and its private learner tombstone."""

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        """Load one report by stable identity."""

    def get_scoped_analytics(
        self,
        report_id: str,
        *,
        course_id: str,
        class_id: str,
    ) -> TeacherAnalyticsBundle | None:
        """Load one report only when its full teaching scope matches."""

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        """Load the newest report within an exact teaching scope."""

    def insert_or_get_review_decision(
        self,
        decision: TeacherReviewDecision,
    ) -> TeacherReviewDecision:
        """Persist a decision idempotently or expose a content conflict."""

    def save_review_decision(self, decision: TeacherReviewDecision) -> None:
        """Persist one optimistic-version teacher decision."""

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        """Load one teacher decision by stable identity."""


def _validate_checksum(value: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise ValueError("M9 model audit checksum must be lowercase SHA-256")


def _json_checksum(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_record_text(value: dict[str, Any], field: str) -> str:
    selected = value.get(field)
    if type(selected) is not str or not selected.strip():
        raise ValueError(f"M9 model audit {field} must not be blank")
    return selected


__all__ = [
    "M9ModelAuditRecord",
    "M9Repository",
    "M9ReviewDecisionConflict",
    "analytics_learner_scope",
]
