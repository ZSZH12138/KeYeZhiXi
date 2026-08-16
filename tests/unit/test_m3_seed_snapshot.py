from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    MAX_ROLE_BYTES,
    M3ValidationReport,
    M3ValidationIssue,
    ROLE_ORDER,
    capture_seed_snapshot,
    create_validation_report,
    seed_snapshot_from_bytes,
    seed_snapshot_to_bytes,
    validation_report_from_bytes,
    validation_report_to_bytes,
)
import course_insight.modules.m3_knowledge_bundle.seed_snapshot as snapshot_module
from course_insight.infrastructure.json_io import dumps_json


def _concept_payload() -> dict[str, object]:
    return {
        "knowledge_bundle_id": "bundle_alpha",
        "bundle_version": "v1",
        "published_at": "2026-08-03T00:00:00+00:00",
        "course_id": "course_alpha",
        "course_package_id": "package_alpha",
        "course_package_checksum": "a" * 64,
        "concepts": [{"concept_id": "concept_1"}],
        "concept_evidence_ids": {"concept_1": ["evidence_chunk_1"]},
    }


def _role_payloads() -> dict[str, dict[str, object]]:
    return {
        "concept": _concept_payload(),
        "item": {"items": [{"item_id": "item_1"}], "q_matrix": []},
        "rubric": {"rubrics": [{"rubric_id": "rubric_1"}]},
        "blueprint": {"blueprints": [{"blueprint_id": "blueprint_1"}]},
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }


def _seed_paths(tmp_path: Path, *, indent: int | None = None) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for role, payload in _role_payloads().items():
        path = tmp_path / f"{role}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
        paths[role] = path
    return paths


def _capture(tmp_path: Path, *, indent: int | None = None):
    paths = _seed_paths(tmp_path, indent=indent)
    return capture_seed_snapshot(
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )


def test_capture_reads_six_roles_once_and_canonicalizes_formatting(tmp_path: Path) -> None:
    paths = _seed_paths(tmp_path, indent=2)
    formatted = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"], misconception_seed_path=paths["misconception"],
    )
    compact = _capture(tmp_path / "compact")
    assert tuple(role.role for role in formatted.roles) == ROLE_ORDER
    assert formatted == compact
    assert all(role.state == "ready" for role in formatted.roles)


def test_optional_none_is_canonical_empty_and_required_root_cannot_be_empty(tmp_path: Path) -> None:
    paths = _seed_paths(tmp_path)
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=None, misconception_seed_path=None,
    )
    assert [(role.role, role.state, role.canonical_json) for role in snapshot.roles[-2:]] == [
        ("prerequisite", "empty", b'{"prerequisite_relations":[]}'),
        ("misconception", "empty", b'{"misconception_tags":[]}'),
    ]
    paths["item"].write_bytes(b"{}")
    invalid = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
    )
    assert invalid.roles[1].state == "invalid"
    assert invalid.roles[1].issue_code == "SEED_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"items":[],"items":[],"q_matrix":[]}',
        b'[1,2,3]',
        b'{"items":[],"q_matrix":[],"extra":true}',
        b'{"items":["\\ud800"],"q_matrix":[]}',
        b'{"items":["bad\\u0000input"],"q_matrix":[]}',
        b"\xff\xfe",
    ],
)
def test_hostile_seed_inputs_become_stable_nonleaking_private_issues(tmp_path: Path, payload: bytes) -> None:
    paths = _seed_paths(tmp_path)
    paths["item"].write_bytes(payload)
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
    )
    role = snapshot.roles[1]
    assert role.state == "invalid"
    assert role.canonical_json is None
    assert role.issue_code in {"SEED_JSON_INVALID", "SEED_SCHEMA_INVALID"}
    persisted = seed_snapshot_to_bytes(snapshot).decode("utf-8")
    assert "bad\\u0000input" not in persisted
    assert "position" not in persisted
    assert str(paths["item"]) not in persisted


def test_snapshot_codec_rejects_noncanonical_forged_and_mutable_values(tmp_path: Path) -> None:
    snapshot = _capture(tmp_path)
    payload = seed_snapshot_to_bytes(snapshot)
    assert seed_snapshot_from_bytes(payload) == snapshot
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(payload.replace(b"{", b"{\n", 1))
    forged = json.loads(payload)
    forged["checksum"] = "0" * 64
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(json.dumps(forged, separators=(",", ":")).encode())
    mutable = json.loads(payload)
    mutable["roles"].reverse()
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(json.dumps(mutable, separators=(",", ":")).encode())
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(b'{"format_version":1,"format_version":1}')
    with pytest.raises(AttributeError):
        snapshot.roles.append(snapshot.roles[0])  # type: ignore[attr-defined]


def test_reports_are_path_and_format_independent_with_strict_biconditional_codecs(tmp_path: Path) -> None:
    first = _capture(tmp_path / "first", indent=2)
    second = _capture(tmp_path / "second")
    assert first.checksum == second.checksum
    approved = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=first, issues=(), knowledge_bundle_id="bundle_alpha",
        bundle_version="v1", bundle_checksum="b" * 64,
    )
    assert approved.status == "approved"
    assert validation_report_from_bytes(validation_report_to_bytes(approved)) == approved
    rejected = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=first,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "", "items"),),
    )
    assert rejected.status == "rejected"
    assert rejected.report_id == approved.report_id
    payload = json.loads(validation_report_to_bytes(rejected))
    payload["status"] = "approved"
    with pytest.raises(ValueError):
        validation_report_from_bytes(json.dumps(payload, separators=(",", ":")).encode())
    payload = json.loads(validation_report_to_bytes(approved))
    payload["issues"] = [{"code": "x", "seed_role": "item", "entity_key": "x", "field": "items"}]
    with pytest.raises(ValueError):
        validation_report_from_bytes(json.dumps(payload, separators=(",", ":")).encode())


def test_capture_enforces_role_and_total_size_limits_without_leaking_paths(tmp_path: Path) -> None:
    paths = _seed_paths(tmp_path)
    paths["item"].write_bytes(b"x" * (1024 * 1024 + 1))
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
    )
    assert snapshot.roles[1].issue_code == "SEED_TOO_LARGE"
    assert str(paths["item"]) not in seed_snapshot_to_bytes(snapshot).decode("utf-8")


@pytest.mark.parametrize("failure_site", ["path", "read"])
def test_capture_sanitizes_arbitrary_path_and_read_exceptions_and_still_reads_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str,
) -> None:
    paths = _seed_paths(tmp_path)
    calls = {path: 0 for path in paths.values()}
    original_open = Path.open

    class ExplodingFile:
        def __enter__(self) -> ExplodingFile:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            del size
            raise RuntimeError("HOST_SECRET C:\\teachers\\seed.json /private/seed.json")

    def counted_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path in calls:
            calls[path] += 1
        if failure_site == "read" and path == paths["item"]:
            return ExplodingFile()
        return original_open(path, mode, *args, **kwargs)

    class ExplodingPath:
        def __fspath__(self) -> str:
            raise RuntimeError("HOST_SECRET C:\\teachers\\seed.json /private/seed.json")

    monkeypatch.setattr(Path, "open", counted_open)
    item_path: Path | object = paths["item"] if failure_site == "read" else ExplodingPath()
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=item_path,  # type: ignore[arg-type]
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"], misconception_seed_path=paths["misconception"],
    )
    assert snapshot.roles[1].issue_code == "SEED_READ_FAILED"
    assert calls[paths["concept"]] == calls[paths["rubric"]] == calls[paths["blueprint"]] == 1
    assert calls[paths["prerequisite"]] == calls[paths["misconception"]] == 1
    persisted = seed_snapshot_to_bytes(snapshot).decode("utf-8")
    assert "HOST_SECRET" not in persisted
    assert "teachers" not in persisted
    assert "private" not in persisted


def test_recursion_inputs_are_stable_private_errors_at_capture_and_codec_boundaries(tmp_path: Path) -> None:
    paths = _seed_paths(tmp_path)
    deep_seed = b'{"items":' + b"[" * 10000 + b"]" * 10000 + b',"q_matrix":[]}'
    paths["item"].write_bytes(deep_seed)
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
    )
    assert snapshot.roles[1].issue_code == "SEED_JSON_INVALID"
    with pytest.raises(ValueError) as snapshot_error:
        seed_snapshot_from_bytes(b'{"roles":' + b"[" * 10000 + b"]" * 10000 + b"}")
    assert snapshot_error.value.__cause__ is None
    with pytest.raises(ValueError) as report_error:
        validation_report_from_bytes(b'{"issues":' + b"[" * 10000 + b"]" * 10000 + b"}")
    assert report_error.value.__cause__ is None


def test_total_limit_is_exercised_without_changing_production_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _seed_paths(tmp_path)
    monkeypatch.setattr(snapshot_module, "MAX_TOTAL_BYTES", 20)
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"], misconception_seed_path=paths["misconception"],
    )
    assert all(role.issue_code == "SEED_TOO_LARGE" for role in snapshot.roles)


def test_codecs_reject_missing_extra_and_rehashed_invariant_forgeries(tmp_path: Path) -> None:
    snapshot = _capture(tmp_path)
    snapshot_wire = json.loads(seed_snapshot_to_bytes(snapshot))
    for mutate in (
        lambda wire: wire.pop("roles"),
        lambda wire: wire.update({"extra": True}),
    ):
        candidate = json.loads(json.dumps(snapshot_wire))
        mutate(candidate)
        with pytest.raises(ValueError):
            seed_snapshot_from_bytes(dumps_json(candidate).encode())
    forged_snapshot = json.loads(seed_snapshot_to_bytes(snapshot))
    forged_snapshot["roles"][1]["content_sha256"] = "0" * 64
    snapshot_core = {key: value for key, value in forged_snapshot.items() if key != "checksum"}
    forged_snapshot["checksum"] = hashlib.sha256(dumps_json(snapshot_core).encode()).hexdigest()
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(dumps_json(forged_snapshot).encode())

    report = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=snapshot,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "", "items"),),
    )
    report_wire = json.loads(validation_report_to_bytes(report))
    for mutate in (
        lambda wire: wire.pop("issues"),
        lambda wire: wire.update({"extra": True}),
    ):
        candidate = json.loads(json.dumps(report_wire))
        mutate(candidate)
        with pytest.raises(ValueError):
            validation_report_from_bytes(dumps_json(candidate).encode())
    forged_report = json.loads(validation_report_to_bytes(report))
    forged_report["status"] = "approved"
    report_core = {key: value for key, value in forged_report.items() if key != "checksum"}
    forged_report["checksum"] = hashlib.sha256(dumps_json(report_core).encode()).hexdigest()
    with pytest.raises(ValueError):
        validation_report_from_bytes(dumps_json(forged_report).encode())


def test_report_identity_and_issue_wire_rules_are_deterministic_and_safe(tmp_path: Path) -> None:
    snapshot = _capture(tmp_path)
    changed_paths = _seed_paths(tmp_path / "changed")
    changed_concept = _concept_payload()
    changed_concept["bundle_version"] = "v2"
    changed_paths["concept"].write_text(json.dumps(changed_concept), encoding="utf-8")
    changed_snapshot = capture_seed_snapshot(
        concept_seed_path=changed_paths["concept"], item_seed_path=changed_paths["item"],
        rubric_seed_path=changed_paths["rubric"], blueprint_seed_path=changed_paths["blueprint"],
        prerequisite_seed_path=changed_paths["prerequisite"], misconception_seed_path=changed_paths["misconception"],
    )
    base = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=snapshot,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0]", "items"),),
    )
    different_package_id = create_validation_report(
        course_package_id="package_beta", course_package_checksum="a" * 64,
        seed_snapshot=snapshot,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0]", "items"),),
    )
    different_package_checksum = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="b" * 64,
        seed_snapshot=snapshot,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0]", "items"),),
    )
    different_snapshot = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=changed_snapshot,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0]", "items"),),
    )
    assert len({
        base.report_id, different_package_id.report_id,
        different_package_checksum.report_id, different_snapshot.report_id,
    }) == 4
    with pytest.raises(ValueError):
        create_validation_report(
            course_package_id="package_alpha", course_package_checksum="a" * 64,
            seed_snapshot=snapshot,
            issues=(
                M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[1]", "items"),
                M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0]", "items"),
            ),
        )
    with pytest.raises(ValueError):
        M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[01]", "items")
    with pytest.raises(ValueError):
        M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0].criteria[0]", "items")
    with pytest.raises(ValueError):
        M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "concept[0]", "items")
    with pytest.raises(ValueError):
        M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "item[0]", "teacher text")
    assert M3ValidationIssue("SEED_SCHEMA_INVALID", "rubric", "rubric[0].criteria[0]", "criteria")


def test_invalid_optional_seed_is_an_invalid_role_not_an_empty_role(tmp_path: Path) -> None:
    paths = _seed_paths(tmp_path)
    paths["misconception"].write_bytes(b"not-json")
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        misconception_seed_path=paths["misconception"],
    )
    assert snapshot.roles[-1].state == "invalid"
    assert snapshot.roles[-1].issue_code == "SEED_JSON_INVALID"


def test_capture_uses_one_strictly_bounded_open_and_read_per_supplied_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = {
        role: dumps_json(payload).encode("utf-8")
        for role, payload in _role_payloads().items()
    }
    opens: dict[str, int] = {role: 0 for role in payloads}
    reads: dict[str, list[int]] = {role: [] for role in payloads}
    conversions: dict[str, int] = {role: 0 for role in payloads}

    class CountingPath:
        def __init__(self, role: str) -> None:
            self.role = role

        def __fspath__(self) -> str:
            conversions[self.role] += 1
            return str(tmp_path / f"{self.role}.json")

    class FakeFile:
        def __init__(self, role: str) -> None:
            self.role = role

        def __enter__(self) -> FakeFile:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            reads[self.role].append(size)
            return payloads[self.role]

    def fake_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> FakeFile:
        del args, kwargs
        assert mode == "rb"
        role = path.stem
        opens[role] += 1
        return FakeFile(role)

    monkeypatch.setattr(Path, "open", fake_open)
    paths = {role: CountingPath(role) for role in payloads}
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"], misconception_seed_path=paths["misconception"],
    )
    assert all(role.state == "ready" for role in snapshot.roles)
    assert conversions == {role: 1 for role in payloads}
    assert opens == {role: 1 for role in payloads}
    assert reads == {role: [MAX_ROLE_BYTES + 1] for role in payloads}


@pytest.mark.parametrize(
    ("knowledge_bundle_id", "bundle_version"),
    [("bundle\x00secret", "v1"), ("bundle", "v1\x00secret"), ("bundle\ud800", "v1"), ("bundle", "v1\ud800")],
)
def test_create_report_rejects_non_utf8_or_nul_approved_text_bindings(
    tmp_path: Path, knowledge_bundle_id: str, bundle_version: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        create_validation_report(
            course_package_id="package_alpha", course_package_checksum="a" * 64,
            seed_snapshot=_capture(tmp_path), issues=(),
            knowledge_bundle_id=knowledge_bundle_id, bundle_version=bundle_version,
            bundle_checksum="b" * 64,
        )
    assert raised.value.__cause__ is None
    assert "secret" not in str(raised.value)


def test_wire_size_limits_apply_before_decode_and_before_encode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _capture(tmp_path)
    report = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=snapshot, issues=(), knowledge_bundle_id="bundle",
        bundle_version="v1", bundle_checksum="b" * 64,
    )
    snapshot_bytes = seed_snapshot_to_bytes(snapshot)
    report_bytes = validation_report_to_bytes(report)
    monkeypatch.setattr(snapshot_module, "MAX_SNAPSHOT_WIRE_BYTES", len(snapshot_bytes) - 1, raising=False)
    with pytest.raises(ValueError):
        seed_snapshot_to_bytes(snapshot)
    original_loads = snapshot_module.json.loads

    def forbidden_decode(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("oversized wire payload reached JSON decoding")

    monkeypatch.setattr(snapshot_module.json, "loads", forbidden_decode)
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(snapshot_bytes)
    monkeypatch.setattr(snapshot_module.json, "loads", original_loads)
    monkeypatch.setattr(snapshot_module, "MAX_REPORT_WIRE_BYTES", len(report_bytes) - 1, raising=False)
    with pytest.raises(ValueError):
        validation_report_to_bytes(report)
    monkeypatch.setattr(snapshot_module.json, "loads", forbidden_decode)
    with pytest.raises(ValueError):
        validation_report_from_bytes(report_bytes)


def test_oversized_role_is_rejected_even_with_rehashed_digest_and_snapshot_checksum(tmp_path: Path) -> None:
    wire = json.loads(seed_snapshot_to_bytes(_capture(tmp_path)))
    wire["roles"][1]["canonical_payload"]["items"] = ["x" * MAX_ROLE_BYTES]
    canonical_payload = dumps_json(wire["roles"][1]["canonical_payload"]).encode("utf-8")
    assert len(canonical_payload) > MAX_ROLE_BYTES
    wire["roles"][1]["content_sha256"] = hashlib.sha256(canonical_payload).hexdigest()
    core = {key: value for key, value in wire.items() if key != "checksum"}
    wire["checksum"] = hashlib.sha256(dumps_json(core).encode()).hexdigest()
    with pytest.raises(ValueError):
        seed_snapshot_from_bytes(dumps_json(wire).encode())


def test_report_codec_explicitly_rejects_noncanonical_duplicate_stale_order_and_mutability(tmp_path: Path) -> None:
    snapshot = _capture(tmp_path)
    issues = (
        M3ValidationIssue("CONCEPT_INVALID", "concept", "concept[0]", "concepts"),
        M3ValidationIssue("ITEM_INVALID", "item", "item[0]", "items"),
    )
    report = create_validation_report(
        course_package_id="package_alpha", course_package_checksum="a" * 64,
        seed_snapshot=snapshot, issues=issues,
    )
    payload = validation_report_to_bytes(report)
    with pytest.raises(ValueError):
        validation_report_from_bytes(payload.replace(b"{", b"{\n", 1))
    with pytest.raises(ValueError):
        validation_report_from_bytes(b'{"format_version":1,' + payload[1:])
    stale = json.loads(payload)
    stale["checksum"] = "0" * 64
    with pytest.raises(ValueError):
        validation_report_from_bytes(dumps_json(stale).encode())
    unordered = json.loads(payload)
    unordered["issues"].reverse()
    unordered_core = {key: value for key, value in unordered.items() if key != "checksum"}
    unordered["checksum"] = hashlib.sha256(dumps_json(unordered_core).encode()).hexdigest()
    with pytest.raises(ValueError):
        validation_report_from_bytes(dumps_json(unordered).encode())
    with pytest.raises(ValueError):
        M3ValidationReport(
            report_id=report.report_id, validator_version=report.validator_version,
            course_package_id=report.course_package_id,
            course_package_checksum=report.course_package_checksum,
            seed_snapshot_checksum=report.seed_snapshot_checksum,
            status=report.status, issues=list(report.issues),  # type: ignore[arg-type]
            knowledge_bundle_id=None, bundle_version=None, bundle_checksum=None,
            checksum=report.checksum,
        )
