"""Replay identity validation for authoritative M6 tutoring decisions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    derive_identifier,
    has_new_evidence,
)


def _identity(**overrides: object) -> EvidenceIdentity:
    values: dict[str, object] = {
        "scoring_result_checksum": "a" * 64,
        "latest_audit_version_keys": ("audit-001:2",),
        "processed_audit_ids": ("audit-001",),
        "learner_state_version": 2,
        "learner_state_checksum": "b" * 64,
    }
    values.update(overrides)
    return EvidenceIdentity(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    (
        {"learner_state_version": 0},
        {"processed_audit_ids": ("audit-001", "audit-001")},
        {"processed_audit_ids": ("audit-002", "audit-001")},
        {"latest_audit_version_keys": ("invalid-version-key",)},
        {"scoring_result_checksum": "A" * 64},
    ),
)
def test_evidence_identity_rejects_noncanonical_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _identity(**overrides)


def test_evidence_identity_from_dict_rejects_invalid_shapes_and_types() -> None:
    payload = _identity().to_dict()

    with pytest.raises(ValueError, match="fields"):
        EvidenceIdentity.from_dict({**payload, "unexpected": True})
    with pytest.raises(ValueError, match="scalar types"):
        EvidenceIdentity.from_dict({**payload, "learner_state_version": True})
    with pytest.raises(ValueError, match="payload"):
        EvidenceIdentity.from_dict({**payload, "processed_audit_ids": "audit-001"})


@pytest.mark.parametrize(
    ("current", "reason"),
    (
        (
            _identity(learner_state_version=1),
            "learner_state_version_regressed",
        ),
        (
            _identity(learner_state_checksum="c" * 64),
            "learner_state_version_content_conflict",
        ),
        (
            _identity(latest_audit_version_keys=("audit-001:1",)),
            "scoring_audit_version_regressed",
        ),
        (
            _identity(scoring_result_checksum="c" * 64),
            "scoring_evidence_content_conflict",
        ),
    ),
)
def test_has_new_evidence_rejects_regression_or_content_conflict(
    current: EvidenceIdentity,
    reason: str,
) -> None:
    with pytest.raises(DomainError) as raised:
        has_new_evidence(current, _identity())

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert raised.value.details == {"reason": reason}


def test_has_new_evidence_detects_each_supported_progress_signal() -> None:
    previous = _identity()

    assert has_new_evidence(previous, None) is False
    assert has_new_evidence(previous, previous) is False
    assert has_new_evidence(replace(previous, learner_state_version=3), previous) is True
    assert has_new_evidence(
        replace(previous, latest_audit_version_keys=("audit-001:3",)),
        previous,
    ) is True
    assert has_new_evidence(
        replace(
            previous,
            latest_audit_version_keys=("audit-001:2", "audit-002:1"),
            processed_audit_ids=("audit-001", "audit-002"),
        ),
        previous,
    ) is True


def test_derived_identifier_rejects_an_invalid_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        derive_identifier("bad/kind", "a" * 64)
