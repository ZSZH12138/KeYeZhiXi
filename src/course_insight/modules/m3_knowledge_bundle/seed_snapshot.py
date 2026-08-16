"""Private, canonical M3 teacher-seed snapshots and validation reports.

This module deliberately retains semantic JSON only: it never keeps source paths,
original whitespace, or parser error text.  The public M3 service consumes these
values in later tasks; nothing here is a public Pydantic contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from course_insight.infrastructure.json_io import dumps_json


FORMAT_VERSION = 1
VALIDATOR_VERSION = "m3_validation_v1"
MAX_ROLE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 6 * 1024 * 1024
# Private persisted-wire caps.  Snapshot allowance covers the fixed role
# envelopes around at most MAX_TOTAL_BYTES of canonical semantic JSON.  Reports
# carry no seed bodies, but use a deliberately conservative bound for issue rows.
MAX_SNAPSHOT_WIRE_BYTES = MAX_TOTAL_BYTES + 512 * 1024
MAX_REPORT_WIRE_BYTES = MAX_TOTAL_BYTES * 4
ROLE_ORDER = (
    "concept",
    "item",
    "rubric",
    "blueprint",
    "prerequisite",
    "misconception",
)
_OPTIONAL_ROLES = frozenset({"prerequisite", "misconception"})
_ROLE_KEYS: dict[str, frozenset[str]] = {
    "concept": frozenset(
        {
            "knowledge_bundle_id", "bundle_version", "published_at", "course_id",
            "course_package_id", "course_package_checksum", "concepts",
            "concept_evidence_ids",
        }
    ),
    "item": frozenset({"items", "q_matrix"}),
    "rubric": frozenset({"rubrics"}),
    "blueprint": frozenset({"blueprints"}),
    "prerequisite": frozenset({"prerequisite_relations"}),
    "misconception": frozenset({"misconception_tags"}),
}
_EMPTY_PAYLOADS = {
    "prerequisite": {"prerequisite_relations": []},
    "misconception": {"misconception_tags": []},
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ISSUE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_ORDINAL = r"(?:0|[1-9]\d*)"
_ENTITY_KEY_RE = re.compile(
    rf"(?:|concept\[{_ORDINAL}\]|item\[{_ORDINAL}\]|rubric\[{_ORDINAL}\](?:\.criteria\[{_ORDINAL}\])?|blueprint\[{_ORDINAL}\]|prerequisite\[{_ORDINAL}\]|misconception\[{_ORDINAL}\])\Z"
)
_ISSUE_FIELDS = frozenset(
    {
        "", "knowledge_bundle_id", "bundle_version", "published_at", "course_id",
        "course_package_id", "course_package_checksum", "concepts",
        "concept_evidence_ids", "items", "q_matrix", "rubrics", "blueprints",
        "prerequisite_relations", "misconception_tags", "concept_id", "item_id",
        "item_version", "rubric_id", "blueprint_id", "anchor_item_ids",
        "anchor_item_versions", "source_evidence_ids", "course_evidence_ids",
        "status", "criteria", "concept_ids", "name", "aliases", "description",
        "relation_type", "from_concept_id", "to_concept_id", "concept_id",
        "misconception_id", "item_ids", "rubric_version", "item_type",
        "difficulty", "max_score", "objective_count", "subjective_count",
        "total_score", "concept_weights", "difficulty_distribution",
        "section_id", "sections", "weight", "score", "version",
        "difficulty_range", "item_count", "item_types",
        "misconception_ids",
    }
)


@dataclass(frozen=True, slots=True)
class SeedRoleSnapshot:
    role: str
    state: Literal["ready", "empty", "invalid"]
    canonical_json: bytes | None
    content_sha256: str | None
    issue_code: str | None

    def __post_init__(self) -> None:
        _validate_role(self)


@dataclass(frozen=True, slots=True)
class M3SeedSnapshot:
    roles: tuple[SeedRoleSnapshot, ...]
    checksum: str

    def __post_init__(self) -> None:
        if type(self.roles) is not tuple:
            raise ValueError("M3 snapshot roles are invalid")


@dataclass(frozen=True, order=True, slots=True)
class M3ValidationIssue:
    code: str
    seed_role: str
    entity_key: str
    field: str

    def __post_init__(self) -> None:
        _validate_issue(self)


@dataclass(frozen=True, slots=True)
class M3ValidationReport:
    report_id: str
    validator_version: str
    course_package_id: str
    course_package_checksum: str
    seed_snapshot_checksum: str
    status: Literal["approved", "rejected"]
    issues: tuple[M3ValidationIssue, ...]
    knowledge_bundle_id: str | None
    bundle_version: str | None
    bundle_checksum: str | None
    checksum: str

    def __post_init__(self) -> None:
        if type(self.issues) is not tuple:
            raise ValueError("M3 report issues are invalid")


def capture_seed_snapshot(
    *,
    concept_seed_path: Path,
    item_seed_path: Path,
    rubric_seed_path: Path,
    blueprint_seed_path: Path,
    prerequisite_seed_path: Path | None = None,
    misconception_seed_path: Path | None = None,
) -> M3SeedSnapshot:
    """Read every supplied seed at most once, then parse the captured bytes.

    Reads are intentionally completed before parsing so a seed cannot observe a
    parse-dependent time-of-check/time-of-use order.  Any bad input is encoded as
    a stable private invalid role, never raised with path or source text.
    """

    paths: dict[str, Path | None] = {
        "concept": concept_seed_path,
        "item": item_seed_path,
        "rubric": rubric_seed_path,
        "blueprint": blueprint_seed_path,
        "prerequisite": prerequisite_seed_path,
        "misconception": misconception_seed_path,
    }
    captured: dict[str, bytes | None] = {}
    for role in ROLE_ORDER:
        path = paths[role]
        if path is None:
            captured[role] = None
            continue
        try:
            resolved_path = Path(path)
            with resolved_path.open("rb") as source:
                raw = source.read(MAX_ROLE_BYTES + 1)
            if type(raw) is not bytes:
                raise TypeError
            captured[role] = raw
        except Exception:
            captured[role] = None

    total_size = sum(len(value) for value in captured.values() if value is not None)
    roles: list[SeedRoleSnapshot] = []
    for role in ROLE_ORDER:
        raw = captured[role]
        if raw is None:
            if paths[role] is None and role in _OPTIONAL_ROLES:
                roles.append(_empty_role(role))
            else:
                roles.append(_invalid_role(role, None, "SEED_READ_FAILED"))
            continue
        if len(raw) > MAX_ROLE_BYTES or total_size > MAX_TOTAL_BYTES:
            roles.append(_invalid_role(role, raw, "SEED_TOO_LARGE"))
            continue
        roles.append(_capture_role(role, raw))
    unchecksummed = M3SeedSnapshot(roles=tuple(roles), checksum="0" * 64)
    return M3SeedSnapshot(
        roles=unchecksummed.roles,
        checksum=_snapshot_checksum(unchecksummed.roles),
    )


def role_payload(snapshot: M3SeedSnapshot, role: str) -> dict[str, Any] | None:
    """Return a fresh, defensive semantic payload for one ready/empty role."""

    _validate_snapshot(snapshot)
    if role not in ROLE_ORDER:
        raise ValueError("M3 seed role is invalid")
    selected = snapshot.roles[ROLE_ORDER.index(role)]
    if selected.canonical_json is None:
        return None
    return _load_json_object(selected.canonical_json, canonical=True)


def seed_snapshot_to_bytes(snapshot: M3SeedSnapshot) -> bytes:
    """Encode a fully revalidated snapshot as exact canonical JSON."""

    _validate_snapshot(snapshot)
    payload = _canonical_bytes(_snapshot_wire(snapshot.roles, snapshot.checksum))
    _require(len(payload) <= MAX_SNAPSHOT_WIRE_BYTES, "M3 snapshot wire payload is too large")
    return payload


def seed_snapshot_from_bytes(payload: bytes) -> M3SeedSnapshot:
    """Decode exactly one canonical persisted M3 snapshot."""

    _require(type(payload) is bytes and len(payload) <= MAX_SNAPSHOT_WIRE_BYTES, "M3 snapshot wire payload is too large")
    value = _load_json_object(payload, canonical=True)
    _require_keys(value, {"checksum", "format_version", "roles"}, "M3 snapshot keys are invalid")
    _require_exact_int(value["format_version"], FORMAT_VERSION, "M3 snapshot format is invalid")
    _require_sha(value["checksum"], "M3 snapshot checksum is invalid")
    _require(type(value["roles"]) is list and len(value["roles"]) == len(ROLE_ORDER), "M3 snapshot roles are invalid")
    roles = tuple(_role_from_wire(entry, expected_role) for entry, expected_role in zip(value["roles"], ROLE_ORDER, strict=True))
    snapshot = M3SeedSnapshot(roles=roles, checksum=value["checksum"])
    _validate_snapshot(snapshot)
    return snapshot


def create_validation_report(
    *,
    course_package_id: str,
    course_package_checksum: str,
    seed_snapshot: M3SeedSnapshot,
    issues: tuple[M3ValidationIssue, ...],
    knowledge_bundle_id: str | None = None,
    bundle_version: str | None = None,
    bundle_checksum: str | None = None,
) -> M3ValidationReport:
    """Build a deterministic report whose status is derived, never caller-chosen."""

    _validate_snapshot(seed_snapshot)
    normalized_issues = tuple(issues)
    _validate_issues(normalized_issues)
    _require_text(course_package_id, "M3 package ID is invalid")
    _require_sha(course_package_checksum, "M3 package checksum is invalid")
    report_id = _report_id(course_package_id, course_package_checksum, seed_snapshot.checksum)
    bindings = (knowledge_bundle_id, bundle_version, bundle_checksum)
    status: Literal["approved", "rejected"]
    if normalized_issues:
        _require(all(value is None for value in bindings), "rejected report bindings are invalid")
        status = "rejected"
    else:
        _require(all(isinstance(value, str) and value for value in bindings), "approved report bindings are invalid")
        _require_text(knowledge_bundle_id, "approved report bundle ID is invalid")
        _require_text(bundle_version, "approved report bundle version is invalid")
        _require_sha(bundle_checksum, "approved report bundle checksum is invalid")
        status = "approved"
    provisional = M3ValidationReport(
        report_id=report_id, validator_version=VALIDATOR_VERSION,
        course_package_id=course_package_id, course_package_checksum=course_package_checksum,
        seed_snapshot_checksum=seed_snapshot.checksum, status=status,
        issues=normalized_issues, knowledge_bundle_id=knowledge_bundle_id,
        bundle_version=bundle_version, bundle_checksum=bundle_checksum, checksum="0" * 64,
    )
    return M3ValidationReport(
        **{**_report_values(provisional), "checksum": _report_checksum(provisional)}
    )


def validation_report_to_bytes(report: M3ValidationReport) -> bytes:
    """Encode a fully revalidated report as exact canonical JSON."""

    _validate_report(report)
    payload = _canonical_bytes(_report_wire(report, report.checksum))
    _require(len(payload) <= MAX_REPORT_WIRE_BYTES, "M3 report wire payload is too large")
    return payload


def validation_report_from_bytes(payload: bytes) -> M3ValidationReport:
    """Decode exactly one canonical persisted M3 validation report."""

    _require(type(payload) is bytes and len(payload) <= MAX_REPORT_WIRE_BYTES, "M3 report wire payload is too large")
    value = _load_json_object(payload, canonical=True)
    required = {
        "format_version", "report_id", "validator_version", "course_package_id",
        "course_package_checksum", "seed_snapshot_checksum", "status", "issues",
        "knowledge_bundle_id", "bundle_version", "bundle_checksum", "checksum",
    }
    _require_keys(value, required, "M3 report keys are invalid")
    _require_exact_int(value["format_version"], FORMAT_VERSION, "M3 report format is invalid")
    _require(type(value["issues"]) is list, "M3 report issues are invalid")
    issues = tuple(_issue_from_wire(item) for item in value["issues"])
    report = M3ValidationReport(
        report_id=value["report_id"], validator_version=value["validator_version"],
        course_package_id=value["course_package_id"], course_package_checksum=value["course_package_checksum"],
        seed_snapshot_checksum=value["seed_snapshot_checksum"], status=value["status"],
        issues=issues, knowledge_bundle_id=value["knowledge_bundle_id"],
        bundle_version=value["bundle_version"], bundle_checksum=value["bundle_checksum"],
        checksum=value["checksum"],
    )
    _validate_report(report)
    return report


def validation_report_core_bytes(report: M3ValidationReport) -> bytes:
    """Return the exact report bytes used for inner semantic equality checks."""

    _validate_report(report)
    return _canonical_bytes(_report_wire(report, None))


def _capture_role(role: str, raw: bytes) -> SeedRoleSnapshot:
    try:
        payload = _load_json_object(raw, canonical=False)
        _require_keys(payload, _ROLE_KEYS[role], "M3 seed schema is invalid")
        canonical = _canonical_bytes(payload)
    except ValueError as error:
        issue = "SEED_SCHEMA_INVALID" if str(error) == "M3 seed schema is invalid" else "SEED_JSON_INVALID"
        return _invalid_role(role, raw, issue)
    return SeedRoleSnapshot(role, "ready", canonical, _sha256(canonical), None)


def _empty_role(role: str) -> SeedRoleSnapshot:
    payload = _EMPTY_PAYLOADS[role]
    canonical = _canonical_bytes(payload)
    return SeedRoleSnapshot(role, "empty", canonical, _sha256(canonical), None)


def _invalid_role(role: str, raw: bytes | None, issue_code: str) -> SeedRoleSnapshot:
    return SeedRoleSnapshot(
        role=role, state="invalid", canonical_json=None,
        content_sha256=_sha256(raw) if raw is not None else None,
        issue_code=issue_code,
    )


def _snapshot_wire(roles: tuple[SeedRoleSnapshot, ...], checksum: str | None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "roles": [_role_wire(role) for role in roles],
    }
    if checksum is not None:
        value["checksum"] = checksum
    return value


def _role_wire(role: SeedRoleSnapshot) -> dict[str, Any]:
    payload = None if role.canonical_json is None else _load_json_object(role.canonical_json, canonical=True)
    return {
        "canonical_payload": payload,
        "content_sha256": role.content_sha256,
        "issue_code": role.issue_code,
        "role": role.role,
        "state": role.state,
    }


def _role_from_wire(value: object, expected_role: str) -> SeedRoleSnapshot:
    _require(type(value) is dict, "M3 snapshot role is invalid")
    _require_keys(value, {"canonical_payload", "content_sha256", "issue_code", "role", "state"}, "M3 snapshot role is invalid")
    _require(value["role"] == expected_role, "M3 snapshot role order is invalid")
    state = value["state"]
    _require(state in {"ready", "empty", "invalid"}, "M3 snapshot role is invalid")
    if value["canonical_payload"] is None:
        canonical = None
    else:
        _require(type(value["canonical_payload"]) is dict, "M3 snapshot payload is invalid")
        canonical = _canonical_bytes(value["canonical_payload"])
    return SeedRoleSnapshot(
        role=expected_role, state=state, canonical_json=canonical,
        content_sha256=value["content_sha256"], issue_code=value["issue_code"],
    )


def _snapshot_checksum(roles: tuple[SeedRoleSnapshot, ...]) -> str:
    return _sha256(_canonical_bytes(_snapshot_wire(roles, None)))


def _validate_snapshot(snapshot: M3SeedSnapshot) -> None:
    _require(isinstance(snapshot, M3SeedSnapshot), "M3 snapshot is invalid")
    _require(type(snapshot.roles) is tuple and len(snapshot.roles) == len(ROLE_ORDER), "M3 snapshot roles are invalid")
    for role, expected_role in zip(snapshot.roles, ROLE_ORDER, strict=True):
        _require(isinstance(role, SeedRoleSnapshot) and role.role == expected_role, "M3 snapshot role order is invalid")
        _validate_role(role)
    _require_sha(snapshot.checksum, "M3 snapshot checksum is invalid")
    _require(snapshot.checksum == _snapshot_checksum(snapshot.roles), "M3 snapshot checksum mismatch")


def _validate_role(role: SeedRoleSnapshot) -> None:
    _require(isinstance(role.role, str) and role.role in ROLE_ORDER, "M3 seed role is invalid")
    _require(isinstance(role.state, str) and role.state in {"ready", "empty", "invalid"}, "M3 seed state is invalid")
    if role.state == "invalid":
        _require(role.canonical_json is None, "invalid M3 seed payload is invalid")
        _require(role.issue_code is not None and bool(_ISSUE_CODE_RE.fullmatch(role.issue_code)), "invalid M3 seed issue is invalid")
        _require(role.content_sha256 is None or _is_sha(role.content_sha256), "invalid M3 seed digest is invalid")
        _require(not (role.role in _OPTIONAL_ROLES and role.issue_code == ""), "invalid M3 seed issue is invalid")
        return
    _require(role.issue_code is None and role.canonical_json is not None, "ready M3 seed payload is invalid")
    _require(type(role.canonical_json) is bytes, "ready M3 seed payload is invalid")
    _require(len(role.canonical_json) <= MAX_ROLE_BYTES, "ready M3 seed payload is too large")
    payload = _load_json_object(role.canonical_json, canonical=True)
    _require_keys(payload, _ROLE_KEYS[role.role], "ready M3 seed schema is invalid")
    _require(role.content_sha256 == _sha256(role.canonical_json), "ready M3 seed digest mismatch")
    if role.state == "empty":
        _require(role.role in _OPTIONAL_ROLES, "required M3 seed cannot be empty")
        _require(payload == _EMPTY_PAYLOADS[role.role], "empty M3 seed payload is invalid")


def _report_values(report: M3ValidationReport) -> dict[str, Any]:
    return {
        "report_id": report.report_id, "validator_version": report.validator_version,
        "course_package_id": report.course_package_id,
        "course_package_checksum": report.course_package_checksum,
        "seed_snapshot_checksum": report.seed_snapshot_checksum,
        "status": report.status, "issues": report.issues,
        "knowledge_bundle_id": report.knowledge_bundle_id,
        "bundle_version": report.bundle_version, "bundle_checksum": report.bundle_checksum,
    }


def _report_wire(report: M3ValidationReport, checksum: str | None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "report_id": report.report_id,
        "validator_version": report.validator_version,
        "course_package_id": report.course_package_id,
        "course_package_checksum": report.course_package_checksum,
        "seed_snapshot_checksum": report.seed_snapshot_checksum,
        "status": report.status,
        "issues": [
            {"code": issue.code, "seed_role": issue.seed_role, "entity_key": issue.entity_key, "field": issue.field}
            for issue in report.issues
        ],
        "knowledge_bundle_id": report.knowledge_bundle_id,
        "bundle_version": report.bundle_version,
        "bundle_checksum": report.bundle_checksum,
    }
    if checksum is not None:
        value["checksum"] = checksum
    return value


def _report_checksum(report: M3ValidationReport) -> str:
    return _sha256(_canonical_bytes(_report_wire(report, None)))


def _report_id(course_package_id: str, course_package_checksum: str, snapshot_checksum: str) -> str:
    return _sha256(_canonical_bytes({
        "course_package_checksum": course_package_checksum,
        "course_package_id": course_package_id,
        "seed_snapshot_checksum": snapshot_checksum,
        "validator_version": VALIDATOR_VERSION,
    }))


def _validate_report(report: M3ValidationReport) -> None:
    _require(isinstance(report, M3ValidationReport), "M3 report is invalid")
    _require(report.validator_version == VALIDATOR_VERSION, "M3 report validator is invalid")
    _require_text(report.course_package_id, "M3 report package ID is invalid")
    _require_sha(report.course_package_checksum, "M3 report package checksum is invalid")
    _require_sha(report.seed_snapshot_checksum, "M3 report snapshot checksum is invalid")
    _require(report.report_id == _report_id(report.course_package_id, report.course_package_checksum, report.seed_snapshot_checksum), "M3 report ID is invalid")
    _validate_issues(report.issues)
    bindings = (report.knowledge_bundle_id, report.bundle_version, report.bundle_checksum)
    if report.status == "approved":
        _require(not report.issues and all(isinstance(value, str) and value for value in bindings), "approved M3 report is invalid")
        _require_text(report.knowledge_bundle_id, "approved M3 report bundle ID is invalid")
        _require_text(report.bundle_version, "approved M3 report bundle version is invalid")
        _require_sha(report.bundle_checksum, "approved M3 report checksum is invalid")
    elif report.status == "rejected":
        _require(bool(report.issues) and all(value is None for value in bindings), "rejected M3 report is invalid")
    else:
        raise ValueError("M3 report status is invalid")
    _require_sha(report.checksum, "M3 report checksum is invalid")
    _require(report.checksum == _report_checksum(report), "M3 report checksum mismatch")


def _validate_issues(issues: tuple[M3ValidationIssue, ...]) -> None:
    _require(type(issues) is tuple, "M3 report issues are invalid")
    for issue in issues:
        _require(isinstance(issue, M3ValidationIssue), "M3 report issue is invalid")
        _validate_issue(issue)
    _require(issues == tuple(sorted(set(issues))), "M3 report issues are not canonical")


def _issue_from_wire(value: object) -> M3ValidationIssue:
    _require(type(value) is dict, "M3 report issue is invalid")
    _require_keys(value, {"code", "seed_role", "entity_key", "field"}, "M3 report issue is invalid")
    return M3ValidationIssue(
        code=value["code"],
        seed_role=value["seed_role"],
        entity_key=value["entity_key"],
        field=value["field"],
    )


def _validate_issue(issue: M3ValidationIssue) -> None:
    _require(isinstance(issue.code, str) and bool(_ISSUE_CODE_RE.fullmatch(issue.code)), "M3 report issue code is invalid")
    _require(isinstance(issue.seed_role, str) and issue.seed_role in ROLE_ORDER, "M3 report issue role is invalid")
    _require(isinstance(issue.entity_key, str) and bool(_ENTITY_KEY_RE.fullmatch(issue.entity_key)), "M3 report issue entity is invalid")
    _require(
        not issue.entity_key or issue.entity_key.startswith(f"{issue.seed_role}["),
        "M3 report issue entity is invalid",
    )
    _require(isinstance(issue.field, str) and issue.field in _ISSUE_FIELDS, "M3 report issue field is invalid")


def _load_json_object(payload: bytes, *, canonical: bool) -> dict[str, Any]:
    _require(type(payload) is bytes, "M3 JSON payload is invalid")
    try:
        text = payload.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        raise ValueError("M3 JSON payload is invalid") from None
    _require(type(value) is dict, "M3 JSON root is invalid")
    try:
        _validate_safe_json_tree(value)
        canonical_payload = _canonical_bytes(value)
    except (ValueError, RecursionError):
        raise ValueError("M3 JSON payload is invalid") from None
    if canonical:
        _require(canonical_payload == payload, "M3 JSON payload is not canonical")
    return value


def _validate_safe_json_tree(value: Any) -> None:
    """Reject strings which JSON can encode but M3 identities must never retain."""

    if isinstance(value, str):
        _require("\x00" not in value, "M3 JSON payload is invalid")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("M3 JSON payload is invalid") from None
        return
    if value is None or type(value) in {bool, int, float}:
        return
    if type(value) is list:
        for item in value:
            _validate_safe_json_tree(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            _validate_safe_json_tree(key)
            _validate_safe_json_tree(item)
        return
    raise ValueError("M3 JSON payload is invalid")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return dumps_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("M3 JSON payload is invalid") from None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _require_sha(value: object, message: str) -> None:
    _require(_is_sha(value), message)


def _require_text(value: object, message: str) -> None:
    _require(isinstance(value, str) and bool(value) and "\x00" not in value, message)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(message) from None


def _require_keys(value: object, keys: set[str] | frozenset[str], message: str) -> None:
    _require(type(value) is dict and set(value) == set(keys), message)


def _require_exact_int(value: object, expected: int, message: str) -> None:
    _require(type(value) is int and value == expected, message)


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


__all__ = [
    "FORMAT_VERSION", "MAX_ROLE_BYTES", "MAX_TOTAL_BYTES", "M3SeedSnapshot",
    "M3ValidationIssue", "M3ValidationReport", "ROLE_ORDER", "SeedRoleSnapshot",
    "VALIDATOR_VERSION", "capture_seed_snapshot", "create_validation_report",
    "role_payload", "seed_snapshot_from_bytes", "seed_snapshot_to_bytes",
    "validation_report_core_bytes", "validation_report_from_bytes",
    "validation_report_to_bytes",
]
