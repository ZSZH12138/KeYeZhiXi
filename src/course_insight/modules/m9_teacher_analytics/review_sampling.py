"""Deterministic, privacy-preserving sampling for the M9 review queue."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from typing import Literal, Mapping

from course_insight.contracts.analytics import ReviewQueueItem
from course_insight.contracts.assessment import (
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.modules.m9_teacher_analytics.reports import latest_audits


REVIEW_SAMPLING_ALGORITHM_VERSION = "hmac-sha256+sha256-v1"
QUALITY_AUDIT_SAMPLE_REASON = "quality_audit_sample"
_CONFIDENCE_BANDS = ("low", "medium", "high")
_KNOWN_STRATA = frozenset(
    f"local_model:{band}"
    for band in _CONFIDENCE_BANDS
)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _valid_rate(value: object) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0.0 <= float(value) <= 1.0
    )


def _normalized_status(record: ScoreAuditRecord) -> str:
    return " ".join(record.review_status.split()).casefold()


def _confidence_band(confidence: float) -> str:
    if confidence < 0.5:
        return "low"
    if confidence < 0.8:
        return "medium"
    return "high"


@dataclass(frozen=True, slots=True)
class ReviewSamplingPolicy:
    """A frozen sampling assignment, independent of input iteration order.

    ``stratum_rates`` overrides the default for a ``method:confidence-band``
    stratum.  The default policy deliberately samples no completed records.
    """

    policy_version: str = "m9-review-sampling-v1"
    algorithm_version: str = REVIEW_SAMPLING_ALGORITHM_VERSION
    hmac_key_id: str = "disabled"
    default_not_required_rate: float = 0.0
    stratum_rates: Mapping[str, float] | tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "policy_version",
            "algorithm_version",
            "hmac_key_id",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.algorithm_version != REVIEW_SAMPLING_ALGORITHM_VERSION:
            raise ValueError("unsupported M9 review-sampling algorithm")
        if not _valid_rate(self.default_not_required_rate):
            raise ValueError("default_not_required_rate must be between 0 and 1")

        raw_items = (
            self.stratum_rates.items()
            if isinstance(self.stratum_rates, Mapping)
            else self.stratum_rates
        )
        normalized: list[tuple[str, float]] = []
        seen: set[str] = set()
        for raw_key, raw_rate in raw_items:
            if type(raw_key) is not str or raw_key not in _KNOWN_STRATA:
                raise ValueError("stratum_rates contains an unknown stratum")
            if raw_key in seen:
                raise ValueError("stratum_rates contains a duplicate stratum")
            if not _valid_rate(raw_rate):
                raise ValueError("stratum sampling rates must be between 0 and 1")
            seen.add(raw_key)
            normalized.append((raw_key, float(raw_rate)))
        object.__setattr__(self, "stratum_rates", tuple(sorted(normalized)))
        object.__setattr__(
            self,
            "default_not_required_rate",
            float(self.default_not_required_rate),
        )
        if (
            self.default_not_required_rate > 0.0
            or any(rate > 0.0 for _, rate in normalized)
        ) and self.hmac_key_id == "disabled":
            raise ValueError("enabled sampling requires a versioned HMAC key ID")

    def rate_for(self, record: ScoreAuditRecord) -> tuple[str, float]:
        """Return the stable stratum and effective completed-record rate."""

        stratum = f"{record.scoring_method}:{_confidence_band(record.confidence)}"
        rates = dict(self.stratum_rates)
        return stratum, rates.get(stratum, self.default_not_required_rate)

    def requires_hmac_key(self) -> bool:
        """Return whether any configured stratum can select a record."""

        return bool(
            self.default_not_required_rate > 0.0
            or any(rate > 0.0 for _, rate in self.stratum_rates)
        )

    def content_checksum(self) -> str:
        """Bind replay records to every policy and algorithm input."""

        return hashlib.sha256(
            _canonical_bytes(
                {
                    "algorithm_version": self.algorithm_version,
                    "default_not_required_rate": self.default_not_required_rate,
                    "hmac_key_id": self.hmac_key_id,
                    "policy_version": self.policy_version,
                    "stratum_rates": dict(self.stratum_rates),
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewSamplingDecision:
    """Replay metadata that never exposes the learner or audit identity."""

    disposition: Literal[
        "required",
        "quality_audit_sample",
        "not_selected",
        "excluded_rejected",
        "excluded_completed",
    ]
    policy_version: str
    policy_checksum: str
    algorithm_version: str
    stratum: str
    effective_rate: float
    sampling_token: str | None

    @property
    def queued(self) -> bool:
        return self.disposition in {"required", "quality_audit_sample"}


def _validated_hmac_key(hmac_key: bytes | bytearray | None) -> bytes:
    if not isinstance(hmac_key, (bytes, bytearray)) or len(hmac_key) < 32:
        raise ValueError("review sampling requires an HMAC key of at least 32 bytes")
    return bytes(hmac_key)


def decide_review_sampling(
    record: ScoreAuditRecord,
    *,
    learner_id: str,
    policy: ReviewSamplingPolicy,
    hmac_key: bytes | bytearray | None = None,
) -> ReviewSamplingDecision:
    """Classify one latest audit and produce a deterministic assignment.

    Pending records bypass sampling, rejected records fail closed, and only the
    exact completed state ``not_required`` is eligible for quality sampling.
    The first layer HMACs identifiers; the second SHA-256 layer combines that
    pseudonym with the frozen policy and stratum.
    """

    if not isinstance(record, ScoreAuditRecord):
        raise TypeError("record must be a ScoreAuditRecord")
    if type(learner_id) is not str or not learner_id.strip():
        raise ValueError("learner_id must not be blank")
    if not isinstance(policy, ReviewSamplingPolicy):
        raise TypeError("policy must be a ReviewSamplingPolicy")

    stratum, rate = policy.rate_for(record)
    common = {
        "policy_version": policy.policy_version,
        "policy_checksum": policy.content_checksum(),
        "algorithm_version": policy.algorithm_version,
        "stratum": stratum,
        "effective_rate": rate,
    }
    if record.is_rejected():
        return ReviewSamplingDecision(
            disposition="excluded_rejected",
            sampling_token=None,
            **common,
        )
    if record.needs_review():
        return ReviewSamplingDecision(
            disposition="required",
            sampling_token=None,
            **common,
        )
    if (
        _normalized_status(record) != "not_required"
        or record.scoring_method != "local_model"
    ):
        return ReviewSamplingDecision(
            disposition="excluded_completed",
            sampling_token=None,
            **common,
        )
    if rate == 0.0:
        return ReviewSamplingDecision(
            disposition="not_selected",
            sampling_token=None,
            **common,
        )

    key = _validated_hmac_key(hmac_key)
    private_identity = hmac.new(
        key,
        _canonical_bytes(
            {
                "audit_id": record.audit_id,
                "audit_version": record.audit_version,
                "attempt_id": record.attempt_id,
                "item_instance_id": record.item_instance_id,
                "learner_id": learner_id,
            }
        ),
        hashlib.sha256,
    ).hexdigest()
    assignment_digest = hashlib.sha256(
        _canonical_bytes(
            {
                "algorithm_version": policy.algorithm_version,
                "identity_hmac": private_identity,
                "policy_checksum": policy.content_checksum(),
                "scoring_method": record.scoring_method,
                "stratum": stratum,
            }
        )
    ).digest()
    bucket = int.from_bytes(assignment_digest[:8], "big") / float(2**64)
    selected = rate == 1.0 or bucket < rate
    return ReviewSamplingDecision(
        disposition=(
            "quality_audit_sample" if selected else "not_selected"
        ),
        sampling_token=assignment_digest.hex(),
        **common,
    )


def build_review_queue(
    scoring: ScoringResultBundle,
    *,
    policy: ReviewSamplingPolicy,
    hmac_key: bytes | bytearray | None = None,
) -> list[ReviewQueueItem]:
    """Build the required plus deterministic quality-audit queue."""

    if not isinstance(scoring, ScoringResultBundle):
        raise TypeError("scoring must be a ScoringResultBundle")
    queue: list[ReviewQueueItem] = []
    for record in latest_audits(scoring):
        decision = decide_review_sampling(
            record,
            learner_id=scoring.learner_id,
            policy=policy,
            hmac_key=hmac_key,
        )
        if not decision.queued:
            continue
        reasons = (
            [QUALITY_AUDIT_SAMPLE_REASON]
            if decision.disposition == "quality_audit_sample"
            else list(record.review_reason) or ["review_required"]
        )
        queue.append(
            ReviewQueueItem(
                audit_id=record.audit_id,
                audit_version=record.audit_version,
                learner_id=scoring.learner_id,
                item_instance_id=record.item_instance_id,
                recommended_score=record.total_score,
                confidence=record.confidence,
                review_reasons=reasons,
            )
        )
    queue.sort(
        key=lambda item: (
            *item.priority_key(),
            item.audit_id,
            item.audit_version,
            item.item_instance_id,
        )
    )
    return queue


DEFAULT_REVIEW_SAMPLING_POLICY = ReviewSamplingPolicy()


__all__ = [
    "DEFAULT_REVIEW_SAMPLING_POLICY",
    "QUALITY_AUDIT_SAMPLE_REASON",
    "REVIEW_SAMPLING_ALGORITHM_VERSION",
    "ReviewSamplingDecision",
    "ReviewSamplingPolicy",
    "build_review_queue",
    "decide_review_sampling",
]
