"""Restart-safe immutable M1 course-package repository tests."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.course import CoursePackage
from course_insight.infrastructure.m1_file_repository import FileM1Repository
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.runtime_artifacts import ImmutableArtifactStore
from course_insight.modules.m1_course_governance.service import M1CourseGovernanceService
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parser(name: str, payload: bytes) -> str:
    del name
    return payload.decode("utf-8")


def _metadata(path: Path) -> Path:
    path.write_bytes(
        json.dumps(
            {
                "course_package_id": "package-1",
                "course_id": "course-1",
                "package_version": "v1",
                "course_name": "Course",
                "imported_at": "2026-08-03T00:00:00+00:00",
            }, separators=(",", ":"),
        ).encode("utf-8")
    )
    return path


def _authorization(path: Path, source: Path) -> Path:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=("file_name", "source_id", "expected_sha256", "authorized_by", "authorized_at", "license_note"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "file_name": source.name,
                "source_id": "source-1",
                "expected_sha256": _sha(source.read_bytes()),
                "authorized_by": "Teacher",
                "authorized_at": "2026-08-01T00:00:00+00:00",
                "license_note": "course use",
            }
        )
    return path


def test_import_publishes_byte_exact_artifact_and_fresh_repository_recovers_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source_bytes = b"First paragraph\r\n\r\nSecond paragraph\n"
    source.write_bytes(source_bytes)
    metadata = _metadata(tmp_path / "metadata.json")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    runtime = tmp_path / "runtime"
    service = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    )

    package = service.import_course([source], metadata, authorization, tmp_path / "out")

    fresh = FileM1Repository(runtime).get_course_package("package-1", "v1")
    assert fresh is not None
    assert fresh.model_dump(mode="json") == package.model_dump(mode="json")
    artifact = next(
        path for path in (runtime / "artifacts" / "m1" / "course_package" / "package-1" / "v1").iterdir()
        if path.is_dir()
    )
    assert (artifact / "sources" / "source-1" / "lesson.txt").read_bytes() == source_bytes
    stored_metadata = json.loads(
        (artifact / "inputs" / "course_metadata.json").read_text(encoding="utf-8")
    )
    original_metadata = json.loads(metadata.read_text(encoding="utf-8"))
    assert {
        key: stored_metadata[key] for key in original_metadata
    } == original_metadata
    assert stored_metadata["parser_metadata"]["source-1"] == {
        "extension": ".txt",
        "media_type": "text/plain",
        "parser_id": "m1.legacy-parser.txt",
        "parser_version": "legacy-v1",
        "capabilities": ["byte-input"],
        "max_bytes": 25 * 1024 * 1024,
    }
    assert (artifact / "inputs" / "source_authorization.csv").read_bytes() == authorization.read_bytes()
    assert json.loads((artifact / "parse_failures.json").read_text(encoding="utf-8")) == []


def test_file_repository_rejects_incomplete_package_only_save(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    package = M1CourseGovernanceService(
        {".txt": _parser}, _sha, _MemoryRepository()
    ).import_course([source], _metadata(tmp_path / "metadata.json"), _authorization(tmp_path / "authorization.csv", source), tmp_path / "out")

    with pytest.raises(DomainError) as raised:
        FileM1Repository(tmp_path / "runtime").save_course_package(package)

    assert raised.value.code == "COURSE_ARTIFACT_INVALID"


def test_service_captures_each_authorized_input_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    metadata = _metadata(tmp_path / "metadata.json")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    reads = {source: 0, metadata: 0, authorization: 0}
    original_open = Path.open

    def counted_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if mode == "rb" and path in reads:
            reads[path] += 1
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(tmp_path / "runtime")
    ).import_course([source], metadata, authorization, tmp_path / "out")

    assert reads == {source: 1, metadata: 1, authorization: 1}


def test_file_repository_returns_none_for_missing_exact_version(tmp_path: Path) -> None:
    assert FileM1Repository(tmp_path / "runtime").get_course_package("package-1", "v1") is None


def test_same_logical_version_rejects_changed_authorization_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    runtime = tmp_path / "runtime"
    metadata = _metadata(tmp_path / "metadata.json")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    service = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    )
    service.import_course([source], metadata, authorization, tmp_path / "first")
    authorization.write_bytes(authorization.read_bytes().replace(b"\r\n", b"\n"))

    with pytest.raises(DomainError) as raised:
        service.import_course([source], metadata, authorization, tmp_path / "second")

    assert raised.value.code == "COURSE_VERSION_CONFLICT"
    identity = runtime / "artifacts" / "m1" / "course_package" / "package-1" / "v1"
    assert len([path for path in identity.iterdir() if path.is_dir()]) == 1


def test_unreadable_authorization_manifest_remains_a_stable_domain_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")

    with pytest.raises(DomainError) as raised:
        M1CourseGovernanceService(
            {".txt": _parser}, _sha, _MemoryRepository()
        ).import_course(
            [source], _metadata(tmp_path / "metadata.json"),
            tmp_path / "missing.csv", tmp_path / "out",
        )

    assert raised.value.code == "UNAUTHORIZED_SOURCE"


def test_service_prefers_complete_repository_without_legacy_save_method(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    repository = _CompleteOnlyRepository()

    M1CourseGovernanceService({".txt": _parser}, _sha, repository).import_course(
        [source], _metadata(tmp_path / "metadata.json"),
        _authorization(tmp_path / "authorization.csv", source), tmp_path / "out",
    )

    assert repository.snapshot is not None
    assert repository.snapshot.source_payloads[0].raw_bytes == b"lesson"


def test_file_repository_accepts_equivalent_zulu_authorization_time(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    authorization.write_bytes(
        authorization.read_bytes().replace(b"2026-08-01T00:00:00+00:00", b"2026-08-01T00:00:00Z")
    )

    package = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(tmp_path / "runtime")
    ).import_course([source], _metadata(tmp_path / "metadata.json"), authorization, tmp_path / "out")

    assert package.status == "ready"


@pytest.mark.parametrize(
    "payload_name",
    [
        "course_package.json",
        "parse_failures.json",
        "inputs/course_metadata.json",
        "inputs/source_authorization.csv",
        "sources/source-1/lesson.txt",
    ],
)
def test_fresh_repository_maps_every_tampered_payload_to_safe_invalid_artifact(
    tmp_path: Path, payload_name: str,
) -> None:
    runtime = _publish_one_source(tmp_path)
    artifact = _artifact_directory(runtime)
    payload = artifact / Path(*payload_name.split("/"))
    original = payload.read_bytes()
    payload.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(DomainError) as raised:
        FileM1Repository(runtime).get_course_package("package-1", "v1")

    assert raised.value.code == "COURSE_ARTIFACT_INVALID"
    assert raised.value.details == {}
    assert str(tmp_path) not in str(raised.value)
    assert str(tmp_path) not in json.dumps(raised.value.details)


def test_same_logical_version_rejects_changed_raw_source_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson\r\n")
    runtime = tmp_path / "runtime"
    metadata = _metadata(tmp_path / "metadata.json")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    service = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    )
    output = tmp_path / "output"
    service.import_course([source], metadata, authorization, output)
    ready_export = (output / "course_package.json").read_bytes()
    source.write_bytes(b"lesson\n")
    _authorization(authorization, source)

    with pytest.raises(DomainError) as raised:
        service.import_course([source], metadata, authorization, output)

    assert raised.value.code == "COURSE_VERSION_CONFLICT"
    assert raised.value.module == "m1"
    assert raised.value.details == {}
    assert str(tmp_path) not in str(raised.value)
    assert (output / "course_package.json").read_bytes() == ready_export
    assert len([path for path in _artifact_directory(runtime).parent.iterdir() if path.is_dir()]) == 1


def test_file_repository_direct_conflict_has_stable_domain_boundary(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"first")
    metadata = _metadata(tmp_path / "metadata.json")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    first = _CapturingCompleteRepository()
    service = M1CourseGovernanceService({".txt": _parser}, _sha, first)
    service.import_course([source], metadata, authorization, tmp_path / "first")
    repository = FileM1Repository(tmp_path / "runtime")
    repository.save_course_import(first.package, first.snapshot)
    source.write_bytes(b"second")
    _authorization(authorization, source)
    second = _CapturingCompleteRepository()
    M1CourseGovernanceService({".txt": _parser}, _sha, second).import_course(
        [source], metadata, authorization, tmp_path / "second",
    )

    with pytest.raises(DomainError) as raised:
        repository.save_course_import(second.package, second.snapshot)

    assert raised.value.code == "COURSE_VERSION_CONFLICT"
    assert raised.value.module == "m1"
    assert raised.value.details == {}
    assert str(tmp_path) not in str(raised.value)


def test_file_repository_rejects_chunk_semantic_forgery_on_save_and_load(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    metadata = _metadata(tmp_path / "metadata.json")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    captured = _CapturingCompleteRepository()
    M1CourseGovernanceService({".txt": _parser}, _sha, captured).import_course(
        [source], metadata, authorization, tmp_path / "out",
    )
    forged = captured.package.model_dump(mode="python")
    forged["content_chunks"][0]["text"] = "forged"
    forged["content_chunks"][0]["sha256"] = _sha(b"forged")
    forged["checksum"] = "pending"
    forged_package = CoursePackage.model_validate(forged)
    forged_package = CoursePackage.model_validate({
        **forged_package.model_dump(mode="python"),
        "checksum": forged_package.recalculate_checksum(),
    })
    repository = FileM1Repository(tmp_path / "direct")

    with pytest.raises(DomainError) as direct:
        repository.save_course_import(forged_package, captured.snapshot)

    assert direct.value.code == "COURSE_ARTIFACT_INVALID"
    runtime = _publish_one_source(tmp_path / "original")
    loaded = ImmutableArtifactStore(runtime).load(
        module="m1", object_type="course_package", object_id="package-1", object_version="v1",
    )
    payloads = dict(loaded.payloads)
    package_data = json.loads(payloads["course_package.json"])
    package_data["content_chunks"][0]["text"] = "forged"
    package_data["content_chunks"][0]["sha256"] = _sha(b"forged")
    forged_loaded = CoursePackage.model_validate(package_data)
    package_data["checksum"] = forged_loaded.recalculate_checksum()
    payloads["course_package.json"] = dumps_json(package_data).encode("utf-8")
    artifact_metadata = {
        **{key: value for key, value in loaded.metadata.items() if key != "sources"},
        "sources": [dict(item) for item in loaded.metadata["sources"]],
    }
    artifact_metadata["package_checksum"] = package_data["checksum"]
    semantic_runtime = tmp_path / "semantic"
    ImmutableArtifactStore(semantic_runtime).publish(
        module="m1", object_type="course_package", object_id="package-1", object_version="v1",
        payloads=payloads, metadata=artifact_metadata,
    )

    with pytest.raises(DomainError) as loaded_error:
        FileM1Repository(semantic_runtime).get_course_package("package-1", "v1")

    assert loaded_error.value.code == "COURSE_ARTIFACT_INVALID"


@pytest.mark.parametrize(
    "snapshot",
    [
        object(),
        None,
        CourseImportSnapshot("not-bytes", b"", ()),  # type: ignore[arg-type]
        CourseImportSnapshot(b"", b"", []),  # type: ignore[arg-type]
        CourseImportSnapshot(b"", b"", (SourcePayload("source-1", "lesson.txt", "not-bytes"),)),  # type: ignore[arg-type]
    ],
)
def test_file_repository_maps_wrong_snapshot_objects_to_safe_invalid_artifact(
    tmp_path: Path, snapshot: object,
) -> None:
    captured = _capture_one_source(tmp_path)

    with pytest.raises(DomainError) as raised:
        FileM1Repository(tmp_path / "runtime").save_course_import(captured.package, snapshot)  # type: ignore[arg-type]

    assert raised.value.code == "COURSE_ARTIFACT_INVALID"
    assert raised.value.details == {}


@pytest.mark.parametrize("legacy", [False, True])
def test_authorization_header_field_order_is_semantic_and_snapshot_stays_exact(
    tmp_path: Path, legacy: bool,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    metadata = _metadata(tmp_path / "metadata.json")
    if legacy:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        data["source_sha256"] = _sha(source.read_bytes())
        metadata.write_text(json.dumps(data), encoding="utf-8")
    fields = ["license_note", "authorized_at", "authorized_by", "source_id", "file_name"]
    if not legacy:
        fields.insert(2, "expected_sha256")
    authorization = tmp_path / "authorization.csv"
    with authorization.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        row = {
            "file_name": "lesson.txt", "source_id": "source-1",
            "expected_sha256": _sha(source.read_bytes()), "authorized_by": "Teacher",
            "authorized_at": "2026-08-01T00:00:00+00:00", "license_note": "course use",
        }
        writer.writerow({key: row[key] for key in fields})
    runtime = tmp_path / "runtime"
    package = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    ).import_course([source], metadata, authorization, tmp_path / "out")

    assert FileM1Repository(runtime).get_course_package("package-1", "v1") == package
    assert (_artifact_directory(runtime) / "inputs" / "source_authorization.csv").read_bytes() == authorization.read_bytes()


def test_metadata_zulu_time_is_semantic_and_persists_across_restart(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    metadata = _metadata(tmp_path / "metadata.json")
    metadata.write_bytes(metadata.read_bytes().replace(b"+00:00", b"Z"))
    runtime = tmp_path / "runtime"
    package = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    ).import_course([source], metadata, _authorization(tmp_path / "authorization.csv", source), tmp_path / "out")

    assert FileM1Repository(runtime).get_course_package("package-1", "v1") == package


@pytest.mark.parametrize("imported_at", ["2026-08-03T00:00:00", "not-a-time"])
def test_direct_save_rejects_naive_or_invalid_metadata_time(tmp_path: Path, imported_at: str) -> None:
    captured = _capture_one_source(tmp_path)
    data = json.loads(captured.snapshot.course_metadata_bytes)
    data["imported_at"] = imported_at
    invalid_snapshot = CourseImportSnapshot(
        dumps_json(data).encode("utf-8"), captured.snapshot.source_authorization_bytes,
        captured.snapshot.source_payloads,
    )

    with pytest.raises(DomainError) as raised:
        FileM1Repository(tmp_path / "runtime").save_course_import(captured.package, invalid_snapshot)

    assert raised.value.code == "COURSE_ARTIFACT_INVALID"


@pytest.mark.parametrize(
    ("file_name", "source_id"),
    [("lesson.txt", "sid."), ("name.txt.", "source-1")],
)
def test_trailing_dot_authorization_identifiers_fail_before_persistence(
    tmp_path: Path, file_name: str, source_id: str,
) -> None:
    source = tmp_path / file_name
    source.write_bytes(b"lesson")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    authorization.write_bytes(
        authorization.read_bytes().replace(b"source-1", source_id.encode("utf-8"))
    )
    output = tmp_path / "out"
    repository = _NeverRepository()

    with pytest.raises(DomainError) as raised:
        M1CourseGovernanceService({".txt": _parser, ".": _parser}, _sha, repository).import_course(
            [source], _metadata(tmp_path / "metadata.json"), authorization, output,
        )

    assert raised.value.code == "UNAUTHORIZED_SOURCE"
    assert repository.calls == 0
    assert not (output / "course_package.json").exists()
    report = (output / "parse_failures.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in str(raised.value)
    assert str(tmp_path) not in report


def test_trailing_dot_source_id_cannot_publish_file_artifact(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    authorization = _authorization(tmp_path / "authorization.csv", source)
    authorization.write_bytes(authorization.read_bytes().replace(b"source-1", b"sid."))
    runtime = tmp_path / "runtime"

    with pytest.raises(DomainError, match="UNAUTHORIZED_SOURCE"):
        M1CourseGovernanceService(
            {".txt": _parser}, _sha, FileM1Repository(runtime)
        ).import_course([source], _metadata(tmp_path / "metadata.json"), authorization, tmp_path / "out")

    assert not (runtime / "artifacts" / "m1" / "course_package" / "package-1" / "v1").exists()


@pytest.mark.parametrize("kind", ["package_checksum", "package_identity", "authorization_hash"])
def test_valid_store_checksum_does_not_bypass_m1_payload_semantics(
    tmp_path: Path, kind: str,
) -> None:
    runtime = _publish_one_source(tmp_path / "original")
    loaded = ImmutableArtifactStore(runtime).load(
        module="m1", object_type="course_package", object_id="package-1", object_version="v1",
    )
    payloads = dict(loaded.payloads)
    metadata = {
        **{key: value for key, value in loaded.metadata.items() if key != "sources"},
        "sources": [dict(item) for item in loaded.metadata["sources"]],
    }
    if kind == "package_checksum":
        package_data = json.loads(payloads["course_package.json"])
        package_data["checksum"] = "0" * 64
        payloads["course_package.json"] = dumps_json(package_data).encode("utf-8")
    elif kind == "package_identity":
        package_data = json.loads(payloads["course_package.json"])
        package_data["course_package_id"] = "other-package"
        candidate = CoursePackage.model_validate(package_data)
        package_data["checksum"] = candidate.recalculate_checksum()
        payloads["course_package.json"] = dumps_json(package_data).encode("utf-8")
    else:
        payloads["inputs/source_authorization.csv"] = payloads[
            "inputs/source_authorization.csv"
        ].replace(loaded.metadata["sources"][0]["sha256"].encode("ascii"), b"0" * 64)
    drifted = tmp_path / f"drifted-{kind}"
    ImmutableArtifactStore(drifted).publish(
        module="m1", object_type="course_package", object_id="package-1", object_version="v1",
        payloads=payloads, metadata=metadata,
    )

    with pytest.raises(DomainError) as raised:
        FileM1Repository(drifted).get_course_package("package-1", "v1")

    assert raised.value.code == "COURSE_ARTIFACT_INVALID"
    assert raised.value.details == {}


def _publish_one_source(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    runtime = tmp_path / "runtime"
    M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    ).import_course(
        [source], _metadata(tmp_path / "metadata.json"),
        _authorization(tmp_path / "authorization.csv", source), tmp_path / "out",
    )
    return runtime


def _capture_one_source(tmp_path: Path) -> "_CapturingCompleteRepository":
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    captured = _CapturingCompleteRepository()
    M1CourseGovernanceService({".txt": _parser}, _sha, captured).import_course(
        [source], _metadata(tmp_path / "metadata.json"),
        _authorization(tmp_path / "authorization.csv", source), tmp_path / "out",
    )
    return captured


def _artifact_directory(runtime: Path) -> Path:
    identity = runtime / "artifacts" / "m1" / "course_package" / "package-1" / "v1"
    return next(path for path in identity.iterdir() if path.is_dir())


def test_fresh_repository_rejects_semantic_drift_despite_valid_store_checksum(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lesson.txt"
    source.write_bytes(b"lesson")
    runtime = tmp_path / "runtime"
    service = M1CourseGovernanceService(
        {".txt": _parser}, _sha, FileM1Repository(runtime)
    )
    service.import_course([source], _metadata(tmp_path / "metadata.json"), _authorization(tmp_path / "authorization.csv", source), tmp_path / "out")
    loaded = ImmutableArtifactStore(runtime).load(
        module="m1", object_type="course_package", object_id="package-1", object_version="v1"
    )
    drifted_runtime = tmp_path / "drifted"
    metadata = {
        **{key: value for key, value in loaded.metadata.items() if key != "sources"},
        "sources": [dict(item) for item in loaded.metadata["sources"]],
    }
    metadata["course_id"] = "different-course"
    ImmutableArtifactStore(drifted_runtime).publish(
        module="m1", object_type="course_package", object_id="package-1", object_version="v1",
        payloads=loaded.payloads, metadata=metadata,
    )

    with pytest.raises(DomainError) as raised:
        FileM1Repository(drifted_runtime).get_course_package("package-1", "v1")

    assert raised.value.code == "COURSE_ARTIFACT_INVALID"


class _MemoryRepository:
    def save_course_package(self, package: object) -> None:
        del package

    def get_course_package(self, course_package_id: str, package_version: str) -> None:
        del course_package_id, package_version
        return None


class _CompleteOnlyRepository:
    def __init__(self) -> None:
        self.snapshot = None

    def save_course_import(self, package: object, snapshot: object) -> None:
        del package
        self.snapshot = snapshot


class _CapturingCompleteRepository:
    def __init__(self) -> None:
        self.package = None
        self.snapshot = None

    def save_course_import(self, package: object, snapshot: object) -> None:
        self.package = package
        self.snapshot = snapshot


class _NeverRepository:
    def __init__(self) -> None:
        self.calls = 0

    def save_course_package(self, package: object) -> None:
        del package
        self.calls += 1
