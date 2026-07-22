"""Canonical identities for replay-safe M6 tutoring decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import StateUpdateResult
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import SessionStateSnapshot


@dataclass(frozen=True, slots=True)
class EvidenceIdentity:
    """The evidence watermark retained with one authoritative M6 decision."""

    scoring_result_checksum: str
    latest_audit_version_keys: tuple[str, ...]
    processed_audit_ids: tuple[str, ...]
    learner_state_version: int
    learner_state_checksum: str

    def __post_init__(self) -> None:
        _require_sha256(self.scoring_result_checksum, "scoring_result_checksum")
        _require_sha256(self.learner_state_checksum, "learner_state_checksum")
        if self.learner_state_version < 1:
            raise ValueError("learner_state_version must be positive")
        for field_name, values in (
            ("latest_audit_version_keys", self.latest_audit_version_keys),
            ("processed_audit_ids", self.processed_audit_ids),
        ):
            if any(not value for value in values) or len(values) != len(set(values)):
                raise ValueError(f"{field_name} must contain unique nonempty values")
            if tuple(sorted(values)) != values:
                raise ValueError(f"{field_name} must use canonical sorted order")
        _audit_versions(self.latest_audit_version_keys)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible identity payload."""

        return {
            "latest_audit_version_keys": list(self.latest_audit_version_keys),
            "learner_state_checksum": self.learner_state_checksum,
            "learner_state_version": self.learner_state_version,
            "processed_audit_ids": list(self.processed_audit_ids),
            "scoring_result_checksum": self.scoring_result_checksum,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EvidenceIdentity":
        """Rebuild a strictly shaped identity from persisted JSON."""

        expected_fields = {
            "latest_audit_version_keys",
            "learner_state_checksum",
            "learner_state_version",
            "processed_audit_ids",
            "scoring_result_checksum",
        }
        if set(payload) != expected_fields:
            raise ValueError("evidence identity fields are invalid")
        if (
            type(payload["scoring_result_checksum"]) is not str
            or type(payload["learner_state_checksum"]) is not str
            or type(payload["learner_state_version"]) is not int
        ):
            raise ValueError("evidence identity scalar types are invalid")
        try:
            return cls(
                scoring_result_checksum=payload["scoring_result_checksum"],
                latest_audit_version_keys=tuple(
                    _string_list(payload["latest_audit_version_keys"])
                ),
                processed_audit_ids=tuple(
                    _string_list(payload["processed_audit_ids"])
                ),
                learner_state_version=payload["learner_state_version"],
                learner_state_checksum=payload["learner_state_checksum"],
            )
        except (TypeError, ValueError) as error:
            raise ValueError("evidence identity payload is invalid") from error

    def fingerprint(self) -> str:
        """Hash this evidence watermark as canonical UTF-8 JSON."""

        return canonical_fingerprint(self.to_dict())


def build_evidence_identity(
    scoring_result_bundle: ScoringResultBundle,
    state_update_result: StateUpdateResult,
) -> EvidenceIdentity:
    """Build the evidence watermark used for progress comparisons."""

    latest_versions: dict[str, int] = {}
    for record in scoring_result_bundle.score_audit_records:
        current = latest_versions.get(record.audit_id)
        if current is None or record.audit_version > current:
            latest_versions = {
                **latest_versions,
                record.audit_id: record.audit_version,
            }
    latest_keys = tuple(
        f"{audit_id}:{latest_versions[audit_id]}"
        for audit_id in sorted(latest_versions)
    )
    learner_state = state_update_result.learner_state_snapshot
    return EvidenceIdentity(
        scoring_result_checksum=scoring_result_bundle.content_checksum(),
        latest_audit_version_keys=latest_keys,
        processed_audit_ids=tuple(sorted(state_update_result.processed_audit_ids)),
        learner_state_version=learner_state.state_version,
        learner_state_checksum=learner_state.content_checksum(),
    )


def has_new_evidence(
    current: EvidenceIdentity,
    previous: EvidenceIdentity | None,
) -> bool:
    """Return whether current evidence advances the previous decision watermark."""

    if previous is None:
        return False
    if current.learner_state_version < previous.learner_state_version:
        _raise_evidence_conflict("learner_state_version_regressed")
    if (
        current.learner_state_version == previous.learner_state_version
        and current.learner_state_checksum != previous.learner_state_checksum
    ):
        _raise_evidence_conflict("learner_state_version_content_conflict")
    current_audits = _audit_versions(current.latest_audit_version_keys)
    previous_audits = _audit_versions(previous.latest_audit_version_keys)
    if any(
        current_audits[audit_id] < previous_audits[audit_id]
        for audit_id in current_audits.keys() & previous_audits.keys()
    ):
        _raise_evidence_conflict("scoring_audit_version_regressed")
    if (
        current.latest_audit_version_keys
        == previous.latest_audit_version_keys
        and current.scoring_result_checksum
        != previous.scoring_result_checksum
    ):
        _raise_evidence_conflict("scoring_evidence_content_conflict")
    return (
        current.learner_state_version > previous.learner_state_version
        or any(
            audit_id not in previous_audits
            or version > previous_audits[audit_id]
            for audit_id, version in current_audits.items()
        )
        or bool(
            set(current.processed_audit_ids) - set(previous.processed_audit_ids)
        )
    )


def request_fingerprint(
    *,
    task_plan: TaskPlan,
    scoring_result_bundle: ScoringResultBundle,
    state_update_result: StateUpdateResult,
    caller_previous_session_state_snapshot: SessionStateSnapshot | None,
) -> str:
    """Hash the exact public API request, including caller history identity."""

    return canonical_fingerprint(
        {
            "caller_previous_session_state_checksum": (
                caller_previous_session_state_snapshot.content_checksum()
                if caller_previous_session_state_snapshot is not None
                else None
            ),
            "identity_version": "m6-request-v1",
            "scoring_result_checksum": scoring_result_bundle.content_checksum(),
            "state_update_checksum": state_update_result.content_checksum(),
            "task_plan_checksum": task_plan.content_checksum(),
        }
    )


def input_fingerprint(
    *,
    task_plan: TaskPlan,
    scoring_result_bundle: ScoringResultBundle,
    state_update_result: StateUpdateResult,
    authoritative_previous_session_state_snapshot: SessionStateSnapshot,
) -> str:
    """Hash inputs after resolving the authoritative previous session cursor."""

    return canonical_fingerprint(
        {
            "authoritative_previous_session_state_checksum": (
                authoritative_previous_session_state_snapshot.content_checksum()
            ),
            "identity_version": "m6-input-v1",
            "scoring_result_checksum": scoring_result_bundle.content_checksum(),
            "state_update_checksum": state_update_result.content_checksum(),
            "task_plan_checksum": task_plan.content_checksum(),
        }
    )


def derive_identifier(kind: str, authoritative_input_fingerprint: str) -> str:
    """Derive a stable opaque identifier without embedding business identities."""

    if not kind or not kind.replace("_", "").isalnum():
        raise ValueError("identifier kind must be a nonempty token")
    _require_sha256(authoritative_input_fingerprint, "input_fingerprint")
    digest = canonical_fingerprint(
        {
            "input_fingerprint": authoritative_input_fingerprint,
            "kind": kind,
        }
    )
    return f"m6_{kind}_{digest}"


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    """Hash a lossless JSON payload with deterministic serialization."""

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _string_list(value: Any) -> list[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise ValueError("value must be a list of strings")
    return list(value)


def _audit_versions(version_keys: tuple[str, ...]) -> dict[str, int]:
    versions: dict[str, int] = {}
    for version_key in version_keys:
        audit_id, separator, version_text = version_key.rpartition(":")
        if (
            not separator
            or not audit_id
            or not version_text.isascii()
            or not version_text.isdecimal()
            or int(version_text) < 1
            or audit_id in versions
        ):
            raise ValueError("audit version keys must contain unique positive versions")
        versions = {**versions, audit_id: int(version_text)}
    return versions


def _require_sha256(value: str, field_name: str) -> None:
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _raise_evidence_conflict(reason: str) -> None:
    raise DomainError(
        code="TUTORING_REFERENCE_MISMATCH",
        module="m6",
        message="tutoring evidence must advance a consistent learner state",
        details={"reason": reason},
    )
