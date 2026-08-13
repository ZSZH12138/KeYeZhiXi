"""Fail-closed active-mode approval gate for M6 learned policies."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
from typing import Any

from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
    PolicyGateResult,
    TutoringPolicyContext,
)


@dataclass(frozen=True, slots=True)
class PolicyGateConfig:
    """Static, finite bounds for one active-policy gate configuration."""

    gate_policy_version: str
    minimum_support: int
    maximum_uncertainty: float
    rollout_percentage: float
    kill_switch: bool

    def __post_init__(self) -> None:
        if not isinstance(self.gate_policy_version, str) or not self.gate_policy_version.strip():
            raise ValueError("gate_policy_version must not be blank")
        if type(self.minimum_support) is not int or self.minimum_support < 0:
            raise ValueError("minimum_support must be a non-negative integer")
        _require_finite_nonnegative(self.maximum_uncertainty, "maximum_uncertainty")
        _require_probability(self.rollout_percentage, "rollout_percentage")
        if type(self.kill_switch) is not bool:
            raise ValueError("kill_switch must be a bool")


class ActivePolicyGate:
    """Evaluate every active-mode condition and return all rejection reasons."""

    def __init__(self, config: PolicyGateConfig) -> None:
        if not isinstance(config, PolicyGateConfig):
            raise ValueError("config must be a PolicyGateConfig")
        self._config = config

    def evaluate(
        self,
        *,
        manifest: PolicyArtifactManifest | None,
        artifact_sha256: str | None,
        feature_schema_version: str | None,
        action_space_version: str | None,
        candidate_count: int | None,
        support: int | None,
        uncertainty: float | None,
        offline_evaluation_approved: bool | None,
        allowed_course_ids: tuple[str, ...],
        allowed_class_ids: tuple[str, ...],
        request_fingerprint: str | None,
        context: TutoringPolicyContext,
    ) -> PolicyGateResult:
        """Fail closed without short-circuiting any applicable gate condition."""

        if manifest is None:
            return PolicyGateResult(False, ("manifest_missing",), self._config.gate_policy_version)
        reasons: list[str] = []
        if manifest.status != "approved":
            reasons.append("manifest_not_approved")
        if manifest.gate_policy_version != self._config.gate_policy_version:
            reasons.append("gate_policy_version_mismatch")
        if artifact_sha256 != manifest.artifact_sha256:
            reasons.append("artifact_sha256_mismatch")
        if feature_schema_version != manifest.feature_schema_version:
            reasons.append("feature_schema_version_mismatch")
        if action_space_version != manifest.action_space_version:
            reasons.append("action_space_version_mismatch")
        if type(candidate_count) is not int or candidate_count < 2:
            reasons.append("candidate_count_too_low")
        if type(support) is not int or support < self._config.minimum_support:
            reasons.append("support_too_low")
        if type(uncertainty) not in {int, float} or not math.isfinite(uncertainty) or uncertainty > self._config.maximum_uncertainty:
            reasons.append("uncertainty_too_high")
        if offline_evaluation_approved is not True:
            reasons.append("offline_evaluation_not_approved")
        if (
            context.course_id is None
            or context.class_id is None
            or context.course_id not in allowed_course_ids
            or context.class_id not in allowed_class_ids
            or f"course:{context.course_id}" not in manifest.allowed_scopes
            or f"class:{context.class_id}" not in manifest.allowed_scopes
        ):
            reasons.append("scope_not_allowed")
        if not isinstance(context, TutoringPolicyContext):
            reasons.append("policy_context_invalid")
        else:
            signals = context.signals
            if any(
                (
                    signals.needs_teacher_review,
                    signals.has_diagnosed_misconception,
                    signals.has_active_misconception,
                    signals.has_prerequisite_gap,
                )
            ):
                reasons.append("remediation_required")
            if request_fingerprint != context.request_fingerprint:
                reasons.append("policy_context_mismatch")
        if self._config.kill_switch:
            reasons.append("kill_switch_enabled")
        if not _in_rollout(request_fingerprint, self._config.rollout_percentage):
            reasons.append("rollout_not_selected")
        return PolicyGateResult(
            allowed=not reasons,
            reasons=tuple(reasons),
            gate_policy_version=self._config.gate_policy_version,
        )


def _in_rollout(request_fingerprint: str | None, percentage: float) -> bool:
    if percentage >= 1.0:
        return True
    if percentage <= 0.0 or not isinstance(request_fingerprint, str) or not request_fingerprint:
        return False
    value = int.from_bytes(sha256(request_fingerprint.encode("utf-8")).digest(), "big")
    return value / (1 << 256) < percentage


def _require_finite_nonnegative(value: Any, name: str) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


def _require_probability(value: Any, name: str) -> None:
    _require_finite_nonnegative(value, name)
    if value > 1.0:
        raise ValueError(f"{name} must be a probability")
