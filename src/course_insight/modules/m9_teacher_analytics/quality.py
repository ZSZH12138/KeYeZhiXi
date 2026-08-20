"""Strict, checksum-bound technical quality evidence for M9 shadow gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import ModelQualityReport


TECHNICAL_QUALITY_EVIDENCE_SCHEMA_VERSION = (
    "m9-technical-quality-evidence-v1"
)
TECHNICAL_QUALITY_METRICS = (
    "citation_precision",
    "fact_fidelity_rate",
    "schema_valid_rate",
    "teacher_acceptance_rate",
    "unsafe_output_rate",
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_EVIDENCE_BYTES = 1024 * 1024
_IDENTITY_FIELDS = (
    "subject_ref",
    "data_id",
    "data_version",
    "data_checksum",
    "model_name",
    "model_version",
    "mode",
    "prompt_id",
    "prompt_version",
    "policy_version",
    "split_id",
    "split_checksum",
)
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_id",
        *_IDENTITY_FIELDS,
        "observation_count",
        "teacher_label_count",
        "metrics",
        "generated_at",
        "payload_checksum",
    }
)


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def technical_quality_payload_checksum(payload: Mapping[str, Any]) -> str:
    """Return the checksum embedded in a technical evidence document."""

    checksum_payload = dict(payload)
    checksum_payload.pop("payload_checksum", None)
    return hashlib.sha256(_canonical_bytes(checksum_payload)).hexdigest()


def _require_nonblank(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


def _require_sha256(value: object, field: str) -> str:
    value = _require_nonblank(value, field)
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _valid_rate(value: object) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0.0 <= float(value) <= 1.0
    )


@dataclass(frozen=True, slots=True)
class TechnicalQualityExpectation:
    """The complete immutable identity expected from one evidence file."""

    subject_ref: str
    data_id: str
    data_version: str
    data_checksum: str
    model_name: str
    model_version: str
    mode: str
    prompt_id: str
    prompt_version: str
    policy_version: str
    split_id: str
    split_checksum: str
    schema_version: str = TECHNICAL_QUALITY_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "subject_ref",
            "data_id",
            "data_version",
            "model_name",
            "model_version",
            "mode",
            "prompt_id",
            "prompt_version",
            "policy_version",
            "split_id",
            "schema_version",
        ):
            _require_nonblank(getattr(self, field_name), field_name)
        _require_sha256(self.data_checksum, "data_checksum")
        _require_sha256(self.split_checksum, "split_checksum")
        if self.schema_version != TECHNICAL_QUALITY_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported technical quality evidence schema")


@dataclass(frozen=True, slots=True)
class TechnicalQualityEvidence:
    """A validated evidence document plus its exact file checksum."""

    schema_version: str
    evidence_id: str
    subject_ref: str
    data_id: str
    data_version: str
    data_checksum: str
    model_name: str
    model_version: str
    mode: str
    prompt_id: str
    prompt_version: str
    policy_version: str
    split_id: str
    split_checksum: str
    observation_count: int
    teacher_label_count: int
    metrics: Mapping[str, float]
    generated_at: datetime
    payload_checksum: str
    file_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(self.metrics)),
        )


@dataclass(frozen=True, slots=True)
class M9QualityGatePolicy:
    """Versioned pre-freeze thresholds; passing permits shadow use only."""

    gate_policy_version: str = "m9-technical-quality-gate-v1"
    minimum_observations: int = 100
    minimum_teacher_labels: int = 30
    minimum_schema_valid_rate: float = 0.99
    minimum_fact_fidelity_rate: float = 0.99
    minimum_citation_precision: float = 0.99
    maximum_unsafe_output_rate: float = 0.0
    minimum_teacher_acceptance_rate: float = 0.80

    def __post_init__(self) -> None:
        _require_nonblank(self.gate_policy_version, "gate_policy_version")
        if (
            type(self.minimum_observations) is not int
            or self.minimum_observations <= 0
            or type(self.minimum_teacher_labels) is not int
            or self.minimum_teacher_labels <= 0
        ):
            raise ValueError("M9 quality-gate sample minima must be positive integers")
        for field_name in (
            "minimum_schema_valid_rate",
            "minimum_fact_fidelity_rate",
            "minimum_citation_precision",
            "maximum_unsafe_output_rate",
            "minimum_teacher_acceptance_rate",
        ):
            if not _valid_rate(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be between 0 and 1")

    def content_checksum(self) -> str:
        return hashlib.sha256(
            _canonical_bytes(
                {
                    "gate_policy_version": self.gate_policy_version,
                    "maximum_unsafe_output_rate": self.maximum_unsafe_output_rate,
                    "minimum_citation_precision": self.minimum_citation_precision,
                    "minimum_fact_fidelity_rate": self.minimum_fact_fidelity_rate,
                    "minimum_observations": self.minimum_observations,
                    "minimum_schema_valid_rate": self.minimum_schema_valid_rate,
                    "minimum_teacher_acceptance_rate": self.minimum_teacher_acceptance_rate,
                    "minimum_teacher_labels": self.minimum_teacher_labels,
                }
            )
        ).hexdigest()


DEFAULT_M9_QUALITY_GATE_POLICY = M9QualityGatePolicy()


def _invalid_evidence(field: str, reason: str) -> DomainError:
    return DomainError(
        code="MODEL_QUALITY_EVIDENCE_INVALID",
        module="m9",
        message="technical model-quality evidence is invalid",
        details={"field": field, "reason": reason},
        recoverable=True,
    )


def _mismatched_evidence(field: str, reason: str = "identity_drift") -> DomainError:
    return DomainError(
        code="MODEL_QUALITY_EVIDENCE_MISMATCH",
        module="m9",
        message="technical model-quality evidence does not match the frozen run",
        details={"field": field, "reason": reason},
        recoverable=True,
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, nested in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = nested
    return value


def _parse_generated_at(value: object) -> datetime:
    if type(value) is not str or not value:
        raise _invalid_evidence("generated_at", "type")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _invalid_evidence("generated_at", "format") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_evidence("generated_at", "timezone_required")
    return parsed


def load_technical_quality_evidence(
    path: Path | str,
    *,
    expected_file_sha256: str,
    expectation: TechnicalQualityExpectation,
) -> TechnicalQualityEvidence:
    """Load evidence only when schema, identities, and both SHA layers match."""

    if not isinstance(expectation, TechnicalQualityExpectation):
        raise TypeError("expectation must be a TechnicalQualityExpectation")
    try:
        expected_digest = _require_sha256(
            expected_file_sha256,
            "expected_file_sha256",
        )
    except ValueError as exc:
        raise _mismatched_evidence(
            "file_sha256",
            "expected_checksum_invalid",
        ) from exc
    try:
        evidence_path = Path(path)
        if evidence_path.is_symlink():
            raise OSError("symlink")
        resolved = evidence_path.resolve(strict=True)
        before = resolved.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_EVIDENCE_BYTES:
            raise OSError("invalid file")
        content = resolved.read_bytes()
        after = resolved.stat()
        if (
            len(content) != before.st_size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise OSError("changed while loading")
    except (OSError, TypeError, ValueError) as exc:
        raise _invalid_evidence("file", "unavailable") from exc
    actual_file_sha256 = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(actual_file_sha256, expected_digest):
        raise _mismatched_evidence("file_sha256", "checksum_mismatch")

    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _invalid_evidence("document", "invalid_json") from exc
    if type(payload) is not dict:
        raise _invalid_evidence("document", "object_required")
    payload_fields = frozenset(payload)
    if payload_fields != _EVIDENCE_FIELDS:
        reason = "missing_or_extra_fields"
        raise _invalid_evidence("document", reason)

    for field_name in ("schema_version", "evidence_id", *_IDENTITY_FIELDS):
        try:
            _require_nonblank(payload[field_name], field_name)
        except ValueError as exc:
            raise _invalid_evidence(field_name, "type_or_blank") from exc
    if payload["schema_version"] != expectation.schema_version:
        raise _mismatched_evidence("schema_version")
    if payload["schema_version"] != TECHNICAL_QUALITY_EVIDENCE_SCHEMA_VERSION:
        raise _invalid_evidence("schema_version", "unsupported")

    for checksum_field in ("data_checksum", "split_checksum", "payload_checksum"):
        try:
            _require_sha256(payload[checksum_field], checksum_field)
        except ValueError as exc:
            raise _invalid_evidence(checksum_field, "sha256_required") from exc
    actual_payload_checksum = technical_quality_payload_checksum(payload)
    if not hmac.compare_digest(
        actual_payload_checksum,
        payload["payload_checksum"],
    ):
        raise _mismatched_evidence("payload_checksum", "checksum_mismatch")

    for field_name in _IDENTITY_FIELDS:
        actual = payload[field_name]
        expected = getattr(expectation, field_name)
        if not hmac.compare_digest(actual, expected):
            raise _mismatched_evidence(field_name)

    observation_count = payload["observation_count"]
    teacher_label_count = payload["teacher_label_count"]
    if type(observation_count) is not int or observation_count < 0:
        raise _invalid_evidence("observation_count", "nonnegative_integer_required")
    if (
        type(teacher_label_count) is not int
        or teacher_label_count < 0
        or teacher_label_count > observation_count
    ):
        raise _invalid_evidence("teacher_label_count", "invalid_count")
    raw_metrics = payload["metrics"]
    if type(raw_metrics) is not dict or set(raw_metrics) != set(
        TECHNICAL_QUALITY_METRICS
    ):
        raise _invalid_evidence("metrics", "exact_metric_set_required")
    metrics: dict[str, float] = {}
    for metric_name in TECHNICAL_QUALITY_METRICS:
        raw_value = raw_metrics[metric_name]
        if not _valid_rate(raw_value):
            raise _invalid_evidence(metric_name, "rate_required")
        metrics[metric_name] = float(raw_value)

    return TechnicalQualityEvidence(
        schema_version=payload["schema_version"],
        evidence_id=payload["evidence_id"],
        subject_ref=payload["subject_ref"],
        data_id=payload["data_id"],
        data_version=payload["data_version"],
        data_checksum=payload["data_checksum"],
        model_name=payload["model_name"],
        model_version=payload["model_version"],
        mode=payload["mode"],
        prompt_id=payload["prompt_id"],
        prompt_version=payload["prompt_version"],
        policy_version=payload["policy_version"],
        split_id=payload["split_id"],
        split_checksum=payload["split_checksum"],
        observation_count=observation_count,
        teacher_label_count=teacher_label_count,
        metrics=metrics,
        generated_at=_parse_generated_at(payload["generated_at"]),
        payload_checksum=payload["payload_checksum"],
        file_sha256=actual_file_sha256,
    )


def build_technical_quality_report(
    evidence: TechnicalQualityEvidence,
    *,
    requested_at: datetime,
    gate_policy: M9QualityGatePolicy = DEFAULT_M9_QUALITY_GATE_POLICY,
) -> ModelQualityReport:
    """Apply the frozen gate without approving, publishing, or writing state."""

    if not isinstance(evidence, TechnicalQualityEvidence):
        raise TypeError("evidence must be TechnicalQualityEvidence")
    if not isinstance(gate_policy, M9QualityGatePolicy):
        raise TypeError("gate_policy must be an M9QualityGatePolicy")
    identity = hashlib.sha256(
        _canonical_bytes(
            {
                "evidence_id": evidence.evidence_id,
                "file_sha256": evidence.file_sha256,
                "gate_policy_checksum": gate_policy.content_checksum(),
                "subject_ref": evidence.subject_ref,
            }
        )
    ).hexdigest()[:24]
    if (
        evidence.observation_count < gate_policy.minimum_observations
        or evidence.teacher_label_count < gate_policy.minimum_teacher_labels
    ):
        return ModelQualityReport(
            report_id=f"quality_insufficient_{identity}",
            subject_ref=evidence.subject_ref,
            metrics={},
            observation_count=evidence.observation_count,
            status="insufficient_data",
            generated_at=requested_at,
        )

    metrics = dict(evidence.metrics)
    passed = bool(
        metrics["schema_valid_rate"]
        >= gate_policy.minimum_schema_valid_rate
        and metrics["fact_fidelity_rate"]
        >= gate_policy.minimum_fact_fidelity_rate
        and metrics["citation_precision"]
        >= gate_policy.minimum_citation_precision
        and metrics["unsafe_output_rate"]
        <= gate_policy.maximum_unsafe_output_rate
        and metrics["teacher_acceptance_rate"]
        >= gate_policy.minimum_teacher_acceptance_rate
    )
    return ModelQualityReport(
        report_id=f"quality_{'ready' if passed else 'failed'}_{identity}",
        subject_ref=evidence.subject_ref,
        metrics=metrics,
        observation_count=evidence.observation_count,
        status="ready" if passed else "failed",
        generated_at=requested_at,
    )


def evaluate_technical_quality_evidence(
    path: Path | str,
    *,
    expected_file_sha256: str,
    expectation: TechnicalQualityExpectation,
    requested_at: datetime,
    gate_policy: M9QualityGatePolicy = DEFAULT_M9_QUALITY_GATE_POLICY,
) -> ModelQualityReport:
    """Strict load plus pure pre-freeze evaluation convenience boundary."""

    evidence = load_technical_quality_evidence(
        path,
        expected_file_sha256=expected_file_sha256,
        expectation=expectation,
    )
    return build_technical_quality_report(
        evidence,
        requested_at=requested_at,
        gate_policy=gate_policy,
    )


__all__ = [
    "DEFAULT_M9_QUALITY_GATE_POLICY",
    "M9QualityGatePolicy",
    "TECHNICAL_QUALITY_EVIDENCE_SCHEMA_VERSION",
    "TECHNICAL_QUALITY_METRICS",
    "TechnicalQualityEvidence",
    "TechnicalQualityExpectation",
    "build_technical_quality_report",
    "evaluate_technical_quality_evidence",
    "load_technical_quality_evidence",
    "technical_quality_payload_checksum",
]
