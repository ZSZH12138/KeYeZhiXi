"""TDD coverage for the S1-S6 SQLite persistence boundary."""

from __future__ import annotations

import csv
import base64
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.application.persistence import (
    BackendReadiness,
    PersistenceBackendSelector,
    PersistenceConfigurationError,
)
from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.m1_m2_m3_repository import (
    SQLiteM1M2M3Repository,
)
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)
from course_insight.modules.m2_evidence_retrieval.lexical import compile_snapshot
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    capture_seed_snapshot,
    create_validation_report,
)


NOW = datetime(2026, 8, 12, tzinfo=timezone.utc)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _course_import(
    *, text: str = "lesson", package_id: str = "package_1"
) -> tuple[CoursePackage, CourseImportSnapshot]:
    raw = text.encode("utf-8")
    source = SourceDocument(
        source_id="source_1",
        file_name="lesson.txt",
        media_type="text/plain",
        sha256=_sha(raw),
        page_count=None,
        title="Course",
        version="v1",
    )
    chunk = ContentChunk(
        chunk_id="chunk_"
        + _sha(f"source_1\0line:1\0{_sha(raw)}".encode("utf-8")),
        source_id="source_1",
        text=text,
        locator="line:1",
        concept_hints=[],
        sha256=_sha(raw),
    )
    candidate = CoursePackage(
        course_package_id=package_id,
        course_id="course_1",
        package_version="v1",
        source_documents=[source],
        content_chunks=[chunk],
        source_authorizations=[
            SourceAuthorization(
                source_id="source_1",
                authorized_by="teacher",
                license_note="course use",
                authorized_at=NOW,
            )
        ],
        imported_at=NOW,
        status="ready",
        checksum="pending",
    )
    package = candidate.model_copy(
        update={"checksum": candidate.recalculate_checksum()}
    )
    metadata = dumps_json(
        {
            "course_package_id": package.course_package_id,
            "course_id": package.course_id,
            "package_version": package.package_version,
            "course_name": "Course",
            "imported_at": NOW.isoformat(),
        }
    ).encode("utf-8")
    authorization = io.StringIO(newline="")
    writer = csv.DictWriter(
        authorization,
        fieldnames=(
            "file_name",
            "source_id",
            "expected_sha256",
            "authorized_by",
            "authorized_at",
            "license_note",
        ),
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerow(
        {
            "file_name": source.file_name,
            "source_id": source.source_id,
            "expected_sha256": source.sha256,
            "authorized_by": "teacher",
            "authorized_at": NOW.isoformat(),
            "license_note": "course use",
        }
    )
    snapshot = CourseImportSnapshot(
        course_metadata_bytes=metadata,
        source_authorization_bytes=authorization.getvalue().encode("utf-8"),
        source_payloads=(
            SourcePayload(
                source_id=source.source_id,
                file_name=source.file_name,
                raw_bytes=raw,
            ),
        ),
    )
    return package, snapshot


def _index_artifact(package: CoursePackage):
    snapshot = compile_snapshot(package)
    from course_insight.contracts.evidence import EvidenceIndexRef

    index = EvidenceIndexRef(
        index_id="index_1",
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        index_version="v1",
        storage_ref="lexical:index_1",
        backend="lexical",
        embedding_model_id=None,
        source_count=len(package.source_documents),
        chunk_count=len(package.content_chunks),
        built_at=NOW,
        checksum=snapshot.checksum,
        status="ready",
    )
    return index, snapshot


def _bundle_artifact(tmp_path: Path, package: CoursePackage):
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    role_payloads: dict[str, dict[str, Any]] = {
        "concept": {
            "knowledge_bundle_id": "bundle_1",
            "bundle_version": "v1",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                {
                    "concept_id": "concept_1",
                    "name": "Lesson",
                    "chapter_id": "chapter_1",
                    "description": "A lesson",
                    "aliases": [],
                    "status": "published",
                }
            ],
            "concept_evidence_ids": {
                "concept_1": [
                    "evidence_" + package.content_chunks[0].chunk_id
                ]
            },
        },
        "item": {
            "items": [
                {
                    "item_id": "item_1",
                    "version": "v1",
                    "stem": "The lesson is valid.",
                    "item_type": "true_false",
                    "concept_ids": ["concept_1"],
                    "misconception_ids": [],
                    "difficulty_level": 1,
                    "cognitive_level": "remember",
                    "parameter_rules": [],
                    "answer_key": {"answer": True, "max_score": 1.0},
                    "rubric_id": None,
                    "source_evidence_ids": [
                        "evidence_" + package.content_chunks[0].chunk_id
                    ],
                    "status": "teacher_approved",
                }
            ],
            "q_matrix": [
                {
                    "item_id": "item_1",
                    "item_version": "v1",
                    "concept_id": "concept_1",
                    "weight": 1.0,
                }
            ],
        },
        "rubric": {"rubrics": []},
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "blueprint_1",
                    "version": "v1",
                    "course_id": package.course_id,
                    "sections": [
                        {
                            "section_id": "section_1",
                            "name": "Lesson",
                            "item_count": 1,
                            "score": 1.0,
                            "item_types": [],
                            "concept_weights": {"concept_1": 1.0},
                            "difficulty_range": [1, 1],
                            "anchor_item_ids": ["item_1"],
                            "anchor_item_versions": {"item_1": "v1"},
                        }
                    ],
                    "total_score": 1.0,
                    "duration_minutes": 30,
                    "status": "teacher_approved",
                }
            ]
        },
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }
    paths: dict[str, Path] = {}
    for role, payload in role_payloads.items():
        path = seed_root / f"{role}.json"
        path.write_bytes(dumps_json(payload).encode("utf-8"))
        paths[role] = path
    seed = capture_seed_snapshot(
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )
    bundle = KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id=package.course_package_id,
        course_id=package.course_id,
        bundle_version="v1",
        concepts=[
            KnowledgeConcept(
                concept_id="concept_1",
                name="Lesson",
                chapter_id="chapter_1",
                description="A lesson",
                aliases=[],
                status="published",
            )
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[
            ItemCard(
                item_id="item_1",
                version="v1",
                stem="The lesson is valid.",
                item_type="true_false",
                concept_ids=["concept_1"],
                misconception_ids=[],
                difficulty_level=1,
                cognitive_level="remember",
                parameter_rules=[],
                answer_key={"answer": True, "max_score": 1.0},
                rubric_id=None,
                source_evidence_ids=[
                    "evidence_" + package.content_chunks[0].chunk_id
                ],
                status="teacher_approved",
            )
        ],
        rubrics=[],
        blueprints=[
            AssessmentBlueprint(
                blueprint_id="blueprint_1",
                version="v1",
                course_id=package.course_id,
                sections=[
                    BlueprintSection(
                        section_id="section_1",
                        name="Lesson",
                        item_count=1,
                        score=1.0,
                        item_types=[],
                        concept_weights={"concept_1": 1.0},
                        difficulty_range=(1, 1),
                        anchor_item_ids=["item_1"],
                        anchor_item_versions={"item_1": "v1"},
                    )
                ],
                total_score=1.0,
                duration_minutes=30,
                status="teacher_approved",
            )
        ],
        q_matrix=[
            {
                "item_id": "item_1",
                "item_version": "v1",
                "concept_id": "concept_1",
                "weight": 1.0,
            }
        ],
        status="published",
        published_at=NOW,
        course_package_checksum=package.checksum,
        concept_evidence_ids={
            "concept_1": [
                "evidence_" + package.content_chunks[0].chunk_id
            ]
        },
    )
    bundle.validate_business_rules()
    report = create_validation_report(
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        seed_snapshot=seed,
        issues=(),
        knowledge_bundle_id=bundle.knowledge_bundle_id,
        bundle_version=bundle.bundle_version,
        bundle_checksum=bundle.content_checksum(),
    )
    return bundle, report, seed


def _repository(tmp_path: Path) -> SQLiteM1M2M3Repository:
    return SQLiteM1M2M3Repository(tmp_path / "runtime.sqlite3")


def test_backend_selection_is_explicit_and_never_falls_back() -> None:
    sqlite_backend = object()
    selected = PersistenceBackendSelector(
        "sqlite",
        sqlite_factory=lambda: sqlite_backend,
        postgresql_factory=lambda: pytest.fail("postgres fallback was used"),
    ).select()
    assert selected is sqlite_backend

    with pytest.raises(PersistenceConfigurationError) as missing:
        PersistenceBackendSelector(
            "postgresql",
            sqlite_factory=lambda: sqlite_backend,
            postgresql_factory=None,
        ).select()
    assert missing.value.code == "PERSISTENCE_BACKEND_UNAVAILABLE"

    def unavailable() -> object:
        raise OSError("backend unavailable")

    with pytest.raises(OSError, match="backend unavailable"):
        PersistenceBackendSelector(
            "postgresql",
            sqlite_factory=lambda: sqlite_backend,
            postgresql_factory=unavailable,
        ).select()


def test_sqlite_repository_round_trips_complete_m1_m2_m3_payloads(
    tmp_path: Path,
) -> None:
    package, course_snapshot = _course_import()
    index, lexical_snapshot = _index_artifact(package)
    bundle, report, seed = _bundle_artifact(tmp_path, package)
    repository = _repository(tmp_path)
    repository.initialize()

    repository.save_course_import(package, course_snapshot)
    repository.save_index_artifact(index, lexical_snapshot)
    repository.save_bundle_artifact(bundle, report, seed)

    fresh = _repository(tmp_path)
    assert fresh.is_ready()
    loaded_package = fresh.load_course_import(
        package.course_package_id, package.package_version
    )
    assert loaded_package == (package, course_snapshot)
    assert fresh.load_index_artifact(index.index_id, index.index_version) == (
        index,
        lexical_snapshot,
    )
    assert fresh.load_bundle_artifact(
        bundle.knowledge_bundle_id, bundle.bundle_version
    ) == (bundle, report, seed)

    manifest = fresh.export_manifest()
    assert manifest["format"] == "s1_s6_repository_manifest/v1"
    assert len(manifest["records"]) == 3
    assert len(manifest["manifest_checksum"]) == 64


def test_same_checksum_is_idempotent_but_same_identity_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    package, snapshot = _course_import()
    repository = _repository(tmp_path)
    repository.initialize()
    repository.save_course_import(package, snapshot)
    repository.save_course_import(package.model_copy(deep=True), snapshot)

    conflicting_package, conflicting_snapshot = _course_import(text="changed")
    with pytest.raises(Exception) as captured:
        repository.save_course_import(conflicting_package, conflicting_snapshot)
    assert getattr(captured.value, "code", None) == "PERSISTENCE_VERSION_CONFLICT"
    assert repository.get_course_package(package.course_package_id, "v1") == package


def test_import_is_validate_then_publish_and_rolls_back_on_any_bad_record(
    tmp_path: Path,
) -> None:
    package, snapshot = _course_import()
    source = _repository(tmp_path / "source")
    source.initialize()
    source.save_course_import(package, snapshot)
    exported = source.export_manifest()

    target = _repository(tmp_path / "target")
    target.initialize()
    target.import_manifest(exported)
    target.import_manifest(exported)
    assert target.get_course_package(package.course_package_id, "v1") == package

    invalid = json.loads(dumps_json(exported))
    invalid["records"].append(
        {
            "module": "m2",
            "object_type": "evidence_index",
            "object_id": "bad",
            "object_version": "v1",
            "status": "ready",
            "content_checksum": "0" * 64,
            "payload_version": "s1_s6/v1",
            "payload_checksum": "0" * 64,
            "payload": {"format": "unknown"},
        }
    )
    unsigned = dict(invalid)
    unsigned.pop("manifest_checksum")
    invalid["manifest_checksum"] = _sha(dumps_json(unsigned).encode("utf-8"))
    with pytest.raises(Exception):
        _repository(tmp_path / "invalid").import_manifest(invalid)

    # A conflict must not publish any rows from the same import transaction.
    conflict_source = _repository(tmp_path / "conflict-source")
    conflict_source.initialize()
    conflict_source.save_course_import(*_course_import(text="changed"))
    conflict_target = _repository(tmp_path / "conflict")
    conflict_target.initialize()
    conflict_target.save_course_import(package, snapshot)
    conflicting = conflict_source.export_manifest()
    with pytest.raises(Exception):
        conflict_target.import_manifest(conflicting)
    assert conflict_target.get_index("missing", "v1") is None


def test_import_rejects_semantic_payload_tamper_even_with_recomputed_outer_checksums(
    tmp_path: Path,
) -> None:
    package, snapshot = _course_import()
    source = _repository(tmp_path / "source")
    source.initialize()
    source.save_course_import(package, snapshot)
    tampered = json.loads(dumps_json(source.export_manifest()))
    envelope = tampered["records"][0]["payload"]
    package_entry = next(
        entry for entry in envelope["payloads"]
        if entry["path"] == "course_package.json"
    )
    package_payload = json.loads(
        base64.b64decode(package_entry["base64"]).decode("utf-8")
    )
    package_payload["course_id"] = "forged_course"
    package_entry["base64"] = base64.b64encode(
        dumps_json(package_payload).encode("utf-8")
    ).decode("ascii")
    tampered["records"][0]["payload_checksum"] = _sha(
        dumps_json(envelope).encode("utf-8")
    )
    unsigned = dict(tampered)
    unsigned.pop("manifest_checksum")
    tampered["manifest_checksum"] = _sha(dumps_json(unsigned).encode("utf-8"))

    target = _repository(tmp_path / "target")
    target.initialize()
    with pytest.raises(Exception):
        target.import_manifest(tampered)
    assert target.export_manifest()["records"] == []


def test_readiness_object_is_safe_and_stable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    assert repository.readiness() == BackendReadiness(
        backend="sqlite", ready=False, reason="schema_unavailable"
    )
    repository.initialize()
    assert repository.readiness() == BackendReadiness(
        backend="sqlite", ready=True, reason=None
    )
