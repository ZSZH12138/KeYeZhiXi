"""Integration coverage for M3's immutable knowledge-artifact repository."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.contracts.course import (
    ContentChunk, CoursePackage, SourceAuthorization, SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.runtime_artifacts import ImmutableArtifactStore
from course_insight.infrastructure.m3_file_repository import FileM3Repository
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot, M3ValidationIssue, M3ValidationReport, capture_seed_snapshot,
    create_validation_report, seed_snapshot_to_bytes, validation_report_to_bytes,
)
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService
from course_insight.modules.m3_knowledge_bundle.stubs import _MemoryM3Repository


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _package() -> CoursePackage:
    raw: dict[str, Any] = {
        "course_package_id": "package_1", "course_id": "course_1", "package_version": "v1",
        "source_documents": [SourceDocument(source_id="source_1", file_name="course.txt", media_type="text/plain", sha256="1" * 64, page_count=1, title="Course", version="v1")],
        "content_chunks": [
            ContentChunk(chunk_id="chunk_1", source_id="source_1", text="one", locator="line:1", concept_hints=["concept_1"], sha256="2" * 64),
            ContentChunk(chunk_id="chunk_2", source_id="source_1", text="two", locator="line:2", concept_hints=["concept_1"], sha256="3" * 64),
        ],
        "source_authorizations": [SourceAuthorization(source_id="source_1", authorized_by="teacher", license_note="course", authorized_at=NOW)],
        "imported_at": NOW, "status": "ready", "checksum": "0" * 64,
    }
    raw["checksum"] = CoursePackage.model_validate(raw).recalculate_checksum()
    return CoursePackage.model_validate(raw)


def _roles(package: CoursePackage, version: str = "v1") -> dict[str, dict[str, Any]]:
    evidence = {"concept_1": ["evidence_chunk_1"]}
    return {
        "concept": {"knowledge_bundle_id": "bundle_1", "bundle_version": version, "published_at": NOW.isoformat(), "course_id": package.course_id, "course_package_id": package.course_package_id, "course_package_checksum": package.checksum, "concepts": [{"concept_id": "concept_1", "name": "Linear equation", "chapter_id": "chapter_1", "description": "Solve equations.", "aliases": ["equation"], "status": "published"}], "concept_evidence_ids": evidence},
        "item": {"items": [{"item_id": "item_1", "version": "v1", "stem": "One plus one equals two.", "item_type": "true_false", "concept_ids": ["concept_1"], "misconception_ids": [], "difficulty_level": 1, "cognitive_level": "remember", "parameter_rules": [], "answer_key": {"answer": True, "max_score": 1.0}, "rubric_id": None, "source_evidence_ids": ["evidence_chunk_1"], "status": "teacher_approved"}], "q_matrix": [{"item_id": "item_1", "item_version": "v1", "concept_id": "concept_1", "weight": 1.0}]},
        "rubric": {"rubrics": []},
        "blueprint": {"blueprints": [{"blueprint_id": "blueprint_1", "version": version, "course_id": package.course_id, "sections": [{"section_id": "section_1", "name": "One", "item_count": 1, "score": 1.0, "item_types": [], "concept_weights": {"concept_1": 1.0}, "difficulty_range": [1, 1], "anchor_item_ids": ["item_1"], "anchor_item_versions": {"item_1": "v1"}}], "total_score": 1.0, "duration_minutes": 30, "status": "teacher_approved"}]},
        "prerequisite": {"prerequisite_relations": []}, "misconception": {"misconception_tags": []},
    }


def _paths(tmp_path: Path, roles: dict[str, dict[str, Any]]) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    result = {}
    for role, value in roles.items():
        path = tmp_path / f"{role}.json"
        path.write_bytes(dumps_json(value).encode())
        result[role] = path
    return result


def _approved(tmp_path: Path, version: str = "v1") -> tuple[CoursePackage, KnowledgeBundle, M3ValidationReport, M3SeedSnapshot]:
    package = _package()
    paths = _paths(tmp_path, _roles(package, version))
    memory = _MemoryM3Repository()
    bundle = M3KnowledgeBundleService(memory, None).build_knowledge_bundle(
        package, paths["concept"], paths["item"], paths["rubric"], paths["blueprint"], paths["prerequisite"], paths["misconception"],
    )
    stored = memory.load_bundle_artifact(bundle.knowledge_bundle_id, bundle.bundle_version)
    assert stored is not None
    return package, *stored


def _rejected(tmp_path: Path) -> tuple[CoursePackage, M3ValidationReport, M3SeedSnapshot]:
    package = _package()
    roles = _roles(package)
    roles["item"]["q_matrix"] = []
    paths = _paths(tmp_path, roles)
    memory = _MemoryM3Repository()
    service = M3KnowledgeBundleService(memory, None)
    with pytest.raises(DomainError) as error:
        service.build_knowledge_bundle(package, paths["concept"], paths["item"], paths["rubric"], paths["blueprint"], paths["prerequisite"], paths["misconception"])
    stored = memory.load_rejected_validation(package.course_package_id, error.value.details["report_id"])
    assert stored is not None
    return package, *stored


def _metadata(bundle: KnowledgeBundle, report: M3ValidationReport, snapshot: M3SeedSnapshot, payloads: dict[str, bytes]) -> dict[str, str]:
    return {
        "format": "m3_knowledge_bundle/v1", "course_package_id": bundle.course_package_id,
        "course_package_checksum": bundle.course_package_checksum,
        "seed_snapshot_checksum": snapshot.checksum, "validation_report_checksum": report.checksum,
        "bundle_checksum": bundle.content_checksum(), "status": "approved",
    }


def _publish_outer_valid(runtime: Path, bundle: KnowledgeBundle, report: M3ValidationReport, snapshot: M3SeedSnapshot) -> None:
    payloads = {
        "bundle.json": dumps_json(bundle.model_dump(mode="json")).encode(),
        "seed_snapshot.json": seed_snapshot_to_bytes(snapshot),
        "validation_report.json": validation_report_to_bytes(report),
    }
    ImmutableArtifactStore(runtime).publish(module="m3", object_type="knowledge_bundle", object_id=bundle.knowledge_bundle_id, object_version=bundle.bundle_version, payloads=payloads, metadata=_metadata(bundle, report, snapshot, payloads))


def _rejection_metadata(report: M3ValidationReport, snapshot: M3SeedSnapshot) -> dict[str, str]:
    return {
        "format": "m3_knowledge_validation/v1", "course_package_id": report.course_package_id,
        "course_package_checksum": report.course_package_checksum,
        "seed_snapshot_checksum": snapshot.checksum,
        "validation_report_checksum": report.checksum, "status": "rejected",
    }


def _assert_safe_invalid(action: Any) -> None:
    with pytest.raises(DomainError) as caught:
        action()
    assert caught.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
    assert caught.value.module == "m3"
    assert caught.value.details == {}
    assert caught.value.__cause__ is None
    assert "\\" not in str(caught.value)


def test_approved_round_trip_is_restart_safe_idempotent_and_versioned(tmp_path: Path) -> None:
    package, bundle, report, snapshot = _approved(tmp_path / "seed")
    repository = FileM3Repository(tmp_path / "runtime")
    assert repository.load_bundle_artifact(bundle.knowledge_bundle_id, bundle.bundle_version) is None
    repository.save_bundle_artifact(bundle, report, snapshot)
    repository.save_bundle_artifact(bundle, report, snapshot)
    restored = FileM3Repository(tmp_path / "runtime").load_bundle_artifact("bundle_1", "v1")
    assert restored is not None and restored[0].model_dump(mode="json") == bundle.model_dump(mode="json")
    _, v2, report2, snapshot2 = _approved(tmp_path / "seed-v2", "v2")
    repository.save_bundle_artifact(v2, report2, snapshot2)
    assert repository.load_bundle_artifact("bundle_1", "v1") is not None
    assert repository.load_bundle_artifact("bundle_1", "v2") is not None
    _assert_safe_invalid(lambda: repository.save_knowledge_bundle(bundle))
    _assert_safe_invalid(lambda: repository.get_knowledge_bundle("bundle_1", "v1"))
    assert package.course_package_id == bundle.course_package_id


def test_subprocess_can_restore_immutable_approved_artifact(tmp_path: Path) -> None:
    _, bundle, report, snapshot = _approved(tmp_path / "seed")
    runtime = tmp_path / "runtime"
    FileM3Repository(runtime).save_bundle_artifact(bundle, report, snapshot)
    statement = "from pathlib import Path; from course_insight.infrastructure.m3_file_repository import FileM3Repository; print(FileM3Repository(Path(sys.argv[1])).load_bundle_artifact('bundle_1','v1') is not None)"
    completed = subprocess.run([sys.executable, "-c", "import sys; " + statement, str(runtime)], capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == "True"


def test_conflict_and_rejected_content_addressing_fail_closed(tmp_path: Path) -> None:
    package, bundle, report, snapshot = _approved(tmp_path / "approved")
    repository = FileM3Repository(tmp_path / "runtime")
    repository.save_bundle_artifact(bundle, report, snapshot)
    altered = bundle.model_copy(deep=True)
    altered.concepts[0].name = "changed"
    altered_report = create_validation_report(course_package_id=package.course_package_id, course_package_checksum=package.checksum, seed_snapshot=snapshot, issues=(), knowledge_bundle_id=altered.knowledge_bundle_id, bundle_version=altered.bundle_version, bundle_checksum=altered.content_checksum())
    with pytest.raises(DomainError) as conflict:
        repository.save_bundle_artifact(altered, altered_report, snapshot)
    assert conflict.value.code == "KNOWLEDGE_VERSION_CONFLICT" and conflict.value.details == {}
    pkg, rejected, rejected_snapshot = _rejected(tmp_path / "rejected")
    repository.save_rejected_validation(rejected, rejected_snapshot)
    repository.save_rejected_validation(rejected, rejected_snapshot)
    assert FileM3Repository(tmp_path / "runtime").load_rejected_validation(pkg.course_package_id, rejected.report_id) is not None
    forged = replace(rejected, checksum="0" * 64)
    _assert_safe_invalid(lambda: repository.save_rejected_validation(forged, rejected_snapshot))
    assert repository.load_rejected_validation(pkg.course_package_id, rejected.report_id) is not None


def test_distinct_valid_rejected_reports_with_one_report_id_never_replace_original(tmp_path: Path) -> None:
    package, original, snapshot = _rejected(tmp_path / "seed")
    replacement = create_validation_report(
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        seed_snapshot=snapshot,
        issues=(M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "", "items"),),
    )
    assert replacement.report_id == original.report_id
    assert validation_report_to_bytes(replacement) != validation_report_to_bytes(original)
    repository = FileM3Repository(tmp_path / "runtime")
    repository.save_rejected_validation(original, snapshot)
    _assert_safe_invalid(lambda: repository.save_rejected_validation(replacement, snapshot))
    loaded = repository.load_rejected_validation(package.course_package_id, original.report_id)
    assert loaded is not None
    assert validation_report_to_bytes(loaded[0]) == validation_report_to_bytes(original)
    assert seed_snapshot_to_bytes(loaded[1]) == seed_snapshot_to_bytes(snapshot)


@pytest.mark.parametrize("kind", ["missing", "extra", "noncanonical", "duplicate"])
def test_rejected_payload_set_and_json_tampering_are_safe_and_fail_closed(tmp_path: Path, kind: str) -> None:
    package, report, snapshot = _rejected(tmp_path / "seed")
    runtime = tmp_path / "runtime"
    repository = FileM3Repository(runtime)
    repository.save_rejected_validation(report, snapshot)
    artifact = next(repository._store._runtime_dir.rglob("manifest.json")).parent
    if kind == "missing":
        (artifact / "seed_snapshot.json").unlink()
    elif kind == "extra":
        (artifact / "bundle.json").write_bytes(b"{}")
    elif kind == "noncanonical":
        path = artifact / "validation_report.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        (artifact / "validation_report.json").write_bytes(b'{"status":"rejected","status":"rejected"}')
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_rejected_validation(package.course_package_id, report.report_id))


def test_outer_valid_rejected_metadata_and_snapshot_binding_tampering_are_safe(tmp_path: Path) -> None:
    package, report, snapshot = _rejected(tmp_path / "seed")
    roles = _roles(package)
    roles["item"]["q_matrix"] = []
    roles["concept"]["concepts"][0]["name"] = "Different but canonical"
    paths = _paths(tmp_path / "other", roles)
    other_snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"], item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"], blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"], misconception_seed_path=paths["misconception"],
    )
    assert other_snapshot.checksum != snapshot.checksum
    payloads = {
        "seed_snapshot.json": seed_snapshot_to_bytes(other_snapshot),
        "validation_report.json": validation_report_to_bytes(report),
    }
    runtime = tmp_path / "binding-runtime"
    store = FileM3Repository(runtime)._store
    store.publish(module="m3", object_type="knowledge_validation", object_id=package.course_package_id, object_version=report.report_id, payloads=payloads, metadata=_rejection_metadata(report, other_snapshot))
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_rejected_validation(package.course_package_id, report.report_id))
    runtime = tmp_path / "metadata-runtime"
    payloads = {
        "seed_snapshot.json": seed_snapshot_to_bytes(snapshot),
        "validation_report.json": validation_report_to_bytes(report),
    }
    metadata = _rejection_metadata(report, snapshot)
    metadata["status"] = "approved"
    FileM3Repository(runtime)._store.publish(module="m3", object_type="knowledge_validation", object_id=package.course_package_id, object_version=report.report_id, payloads=payloads, metadata=metadata)
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_rejected_validation(package.course_package_id, report.report_id))


@pytest.mark.parametrize("kind", ["bundle.json", "seed_snapshot.json", "validation_report.json", "manifest.json", "metadata", "missing", "extra", "noncanonical", "duplicate"])
def test_outer_and_payload_tampering_is_rejected_without_paths(tmp_path: Path, kind: str) -> None:
    _, bundle, report, snapshot = _approved(tmp_path / "seed")
    runtime = tmp_path / "runtime"
    repository = FileM3Repository(runtime)
    repository.save_bundle_artifact(bundle, report, snapshot)
    artifact = next(runtime.rglob("manifest.json")).parent
    if kind == "missing":
        (artifact / "bundle.json").unlink()
    elif kind == "extra":
        (artifact / "surplus.json").write_bytes(b"{}")
    elif kind == "noncanonical":
        (artifact / "bundle.json").write_bytes((artifact / "bundle.json").read_bytes() + b"\n")
    elif kind == "duplicate":
        (artifact / "bundle.json").write_bytes(b'{"knowledge_bundle_id":"bundle_1","knowledge_bundle_id":"bundle_1"}')
    elif kind == "metadata":
        path = artifact / "manifest.json"
        path.write_bytes(path.read_bytes().replace(b"m3_knowledge_bundle/v1", b"m3_knowledge_bundle/x1"))
    else:
        path = artifact / kind
        path.write_bytes(path.read_bytes() + b"x")
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_bundle_artifact("bundle_1", "v1"))


def test_outer_valid_metadata_and_cross_payload_semantic_forgery_are_rejected(tmp_path: Path) -> None:
    _, bundle, report, snapshot = _approved(tmp_path / "v1")
    _, other_bundle, other_report, other_snapshot = _approved(tmp_path / "v2", "v2")
    payloads = {
        "bundle.json": dumps_json(bundle.model_dump(mode="json")).encode(),
        "seed_snapshot.json": seed_snapshot_to_bytes(other_snapshot),
        "validation_report.json": validation_report_to_bytes(other_report),
    }
    runtime = tmp_path / "cross-runtime"
    metadata = _metadata(bundle, other_report, other_snapshot, payloads)
    ImmutableArtifactStore(runtime).publish(module="m3", object_type="knowledge_bundle", object_id="bundle_1", object_version="v1", payloads=payloads, metadata=metadata)
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_bundle_artifact("bundle_1", "v1"))
    runtime = tmp_path / "metadata-runtime"
    payloads = {
        "bundle.json": dumps_json(bundle.model_dump(mode="json")).encode(),
        "seed_snapshot.json": seed_snapshot_to_bytes(snapshot),
        "validation_report.json": validation_report_to_bytes(report),
    }
    metadata = _metadata(bundle, report, snapshot, payloads)
    metadata["format"] = "m3_knowledge_bundle/v9"
    ImmutableArtifactStore(runtime).publish(module="m3", object_type="knowledge_bundle", object_id="bundle_1", object_version="v1", payloads=payloads, metadata=metadata)
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_bundle_artifact("bundle_1", "v1"))
    assert other_bundle.bundle_version == "v2"


@pytest.mark.parametrize("field", ["status", "checksum", "course_package_id", "course_package_checksum", "bundle_checksum", "seed_snapshot_checksum"])
def test_rehashed_forged_report_fields_are_rejected(tmp_path: Path, field: str) -> None:
    _, bundle, report, snapshot = _approved(tmp_path / "seed")
    wire = json.loads(validation_report_to_bytes(report))
    wire[field] = "rejected" if field == "status" else ("0" * 64 if "checksum" in field else "other")
    if field != "checksum":
        core = {key: value for key, value in wire.items() if key != "checksum"}
        wire["checksum"] = hashlib.sha256(dumps_json(core).encode()).hexdigest()
    forged = dumps_json(wire).encode()
    runtime = tmp_path / "runtime"
    payloads = {"bundle.json": dumps_json(bundle.model_dump(mode="json")).encode(), "seed_snapshot.json": seed_snapshot_to_bytes(snapshot), "validation_report.json": forged}
    meta = _metadata(bundle, report, snapshot, payloads)
    meta["validation_report_checksum"] = wire["checksum"]
    ImmutableArtifactStore(runtime).publish(module="m3", object_type="knowledge_bundle", object_id="bundle_1", object_version="v1", payloads=payloads, metadata=meta)
    _assert_safe_invalid(lambda: FileM3Repository(runtime).load_bundle_artifact("bundle_1", "v1"))


def test_service_restore_recomputes_coordinated_inner_semantics_after_outer_validation(tmp_path: Path) -> None:
    package, bundle, report, snapshot = _approved(tmp_path / "seed")
    forged_bundle = bundle.model_copy(deep=True)
    forged_bundle.concepts[0].name = "outer-valid but not seed-derived"
    forged_report = create_validation_report(course_package_id=report.course_package_id, course_package_checksum=report.course_package_checksum, seed_snapshot=snapshot, issues=(), knowledge_bundle_id=forged_bundle.knowledge_bundle_id, bundle_version=forged_bundle.bundle_version, bundle_checksum=forged_bundle.content_checksum())
    runtime = tmp_path / "runtime"
    _publish_outer_valid(runtime, forged_bundle, forged_report, snapshot)
    service = M3KnowledgeBundleService(FileM3Repository(runtime), None)
    _assert_safe_invalid(lambda: service.restore_knowledge_bundle(course_package=package, knowledge_bundle_id="bundle_1", bundle_version="v1"))


@pytest.mark.parametrize("identity", [(" bad", "v1"), ("bundle/escape", "v1"), ("bundle", ".."), ("bundle", "v1\\escape")])
def test_invalid_path_segments_and_repository_errors_are_safe(tmp_path: Path, identity: tuple[str, str]) -> None:
    repository = FileM3Repository(tmp_path / "runtime")
    _assert_safe_invalid(lambda: repository.load_bundle_artifact(*identity))
    _assert_safe_invalid(lambda: repository.load_rejected_validation(identity[0], "report/escape"))
