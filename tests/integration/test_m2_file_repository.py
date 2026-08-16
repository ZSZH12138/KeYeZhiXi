"""Persistent M2 lexical artifact boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.runtime_artifacts import ImmutableArtifactStore
from course_insight.modules.m2_evidence_retrieval.lexical import compile_snapshot, snapshot_to_payloads


def _package(*, version: str = "v1", text: str = "course rule") -> CoursePackage:
    chunk = ContentChunk(
        chunk_id="chunk_alpha",
        source_id="source_alpha",
        text=text,
        locator="paragraph:1",
        concept_hints=["concept_alpha"],
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    candidate = CoursePackage(
        course_package_id="course_package_alpha",
        course_id="course_alpha",
        package_version=version,
        source_documents=[SourceDocument(
            source_id="source_alpha", file_name="course.md", media_type="text/markdown",
            sha256="a" * 64, page_count=None, title="Course", version=version,
        )],
        content_chunks=[chunk],
        source_authorizations=[SourceAuthorization(
            source_id="source_alpha", authorized_by="teacher", license_note="allowed",
            authorized_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )],
        imported_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(update={"checksum": candidate.recalculate_checksum()})


def _ready_index_and_snapshot(*, version: str = "v1", text: str = "course rule"):
    package = _package(version=version, text=text)
    snapshot = compile_snapshot(package)
    index = EvidenceIndexRef(
        index_id="course_package_alpha_lexical_index",
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        index_version=version,
        storage_ref="lexical:course_package_alpha_lexical_index",
        backend="lexical",
        embedding_model_id=None,
        source_count=1,
        chunk_count=1,
        built_at=package.imported_at,
        checksum=snapshot.checksum,
        status="ready",
    )
    return index, snapshot


def _assert_invalid(action: object) -> None:
    with pytest.raises(DomainError) as captured:
        action()  # type: ignore[operator]
    assert captured.value.code == "INDEX_ARTIFACT_INVALID"
    assert captured.value.details == {}
    assert "C:" not in str(captured.value)


def test_file_repository_recovers_complete_snapshot_in_fresh_instance(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    index, snapshot = _ready_index_and_snapshot()
    FileM2Repository(tmp_path / "runtime").save_index_artifact(index, snapshot)
    restored = FileM2Repository(tmp_path / "runtime").load_index_artifact(
        index.index_id, index.index_version
    )
    assert restored == (index, snapshot)


def test_file_repository_recovers_in_a_fresh_subprocess(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    runtime = tmp_path / "runtime"
    index, snapshot = _ready_index_and_snapshot()
    FileM2Repository(runtime).save_index_artifact(index, snapshot)
    program = (
        "from pathlib import Path\n"
        "from course_insight.infrastructure.m2_file_repository import FileM2Repository\n"
        f"loaded = FileM2Repository(Path({str(runtime)!r})).load_index_artifact({index.index_id!r}, {index.index_version!r})\n"
        "assert loaded is not None\n"
        "print(loaded[0].checksum)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == index.checksum


def test_file_repository_missing_is_none_and_identical_retry_is_idempotent(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    repository = FileM2Repository(tmp_path / "runtime")
    assert repository.load_index_artifact("missing", "v1") is None
    index, snapshot = _ready_index_and_snapshot()
    repository.save_index_artifact(index, snapshot)
    repository.save_index_artifact(index.model_copy(deep=True), snapshot)
    assert repository.get_index(index.index_id, index.index_version) == index


def test_file_repository_rejects_package_only_save_and_conflicting_snapshot(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    repository = FileM2Repository(tmp_path / "runtime")
    index, snapshot = _ready_index_and_snapshot(text="first")
    _assert_invalid(lambda: repository.save_index(index))
    repository.save_index_artifact(index, snapshot)
    changed_index, changed_snapshot = _ready_index_and_snapshot(text="changed")
    with pytest.raises(DomainError) as captured:
        repository.save_index_artifact(changed_index, changed_snapshot)
    assert captured.value.code == "INDEX_VERSION_CONFLICT"
    assert captured.value.details == {}


@pytest.mark.parametrize("payload_name", ["index_ref.json", "documents.json", "postings.json"])
def test_file_repository_maps_each_payload_tamper_to_safe_invalid(
    tmp_path: Path, payload_name: str
) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    runtime = tmp_path / "runtime"
    index, snapshot = _ready_index_and_snapshot()
    repository = FileM2Repository(runtime)
    repository.save_index_artifact(index, snapshot)
    loaded = ImmutableArtifactStore(runtime).load(
        module="m2", object_type="evidence_index", object_id=index.index_id,
        object_version=index.index_version,
    )
    payloads = dict(loaded.payloads)
    payloads[payload_name] = b"{}"
    poisoned = tmp_path / "poisoned"
    ImmutableArtifactStore(poisoned).publish(
        module="m2", object_type="evidence_index", object_id=index.index_id,
        object_version=index.index_version, payloads=payloads, metadata=dict(loaded.metadata),
    )
    _assert_invalid(lambda: FileM2Repository(poisoned).load_index_artifact(index.index_id, index.index_version))


def test_file_repository_rejects_semantic_forgery_with_valid_outer_checksum(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    index, snapshot = _ready_index_and_snapshot()
    payloads = snapshot_to_payloads(snapshot)
    ref = index.model_dump(mode="json")
    ref["checksum"] = "0" * 64
    payloads = {**payloads, "index_ref.json": dumps_json(ref).encode("utf-8")}
    metadata = {
        "format": "m2_evidence_index/v1",
        "course_package_id": index.course_package_id,
        "course_package_checksum": index.course_package_checksum,
        "index_checksum": index.checksum,
        "tokenizer_version": snapshot.tokenizer_version,
        "documents_sha256": hashlib.sha256(payloads["documents.json"]).hexdigest(),
        "postings_sha256": hashlib.sha256(payloads["postings.json"]).hexdigest(),
    }
    runtime = tmp_path / "runtime"
    ImmutableArtifactStore(runtime).publish(
        module="m2", object_type="evidence_index", object_id=index.index_id,
        object_version=index.index_version, payloads=payloads, metadata=metadata,
    )
    _assert_invalid(lambda: FileM2Repository(runtime).load_index_artifact(index.index_id, index.index_version))


@pytest.mark.parametrize(
    "forgery", ["tokenizer", "ref_package", "ref_package_checksum", "postings_order"]
)
def test_file_repository_rejects_semantic_forgery_with_valid_outer_artifact(
    tmp_path: Path, forgery: str
) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    index, snapshot = _ready_index_and_snapshot()
    payloads = {
        **snapshot_to_payloads(snapshot),
        "index_ref.json": dumps_json(index.model_dump(mode="json")).encode("utf-8"),
    }
    if forgery == "tokenizer":
        documents = json.loads(payloads["documents.json"])
        documents["tokenizer_version"] = "m2_other_v1"
        payloads["documents.json"] = dumps_json(documents).encode("utf-8")
    elif forgery == "ref_package":
        ref = json.loads(payloads["index_ref.json"])
        ref["course_package_id"] = "other_package"
        payloads["index_ref.json"] = dumps_json(ref).encode("utf-8")
    elif forgery == "ref_package_checksum":
        ref = json.loads(payloads["index_ref.json"])
        ref["course_package_checksum"] = "0" * 64
        payloads["index_ref.json"] = dumps_json(ref).encode("utf-8")
    else:
        postings = json.loads(payloads["postings.json"])
        postings["postings"].reverse()
        payloads["postings.json"] = dumps_json(postings).encode("utf-8")
    metadata = {
        "format": "m2_evidence_index/v1",
        "course_package_id": index.course_package_id,
        "course_package_checksum": (
            "0" * 64 if forgery == "ref_package_checksum" else index.course_package_checksum
        ),
        "index_checksum": index.checksum,
        "tokenizer_version": snapshot.tokenizer_version,
        "documents_sha256": hashlib.sha256(payloads["documents.json"]).hexdigest(),
        "postings_sha256": hashlib.sha256(payloads["postings.json"]).hexdigest(),
    }
    runtime = tmp_path / "runtime"
    ImmutableArtifactStore(runtime).publish(
        module="m2", object_type="evidence_index", object_id=index.index_id,
        object_version=index.index_version, payloads=payloads, metadata=metadata,
    )
    _assert_invalid(lambda: FileM2Repository(runtime).load_index_artifact(index.index_id, index.index_version))


@pytest.mark.parametrize("payloads_kind", ["missing", "extra"])
def test_file_repository_rejects_missing_or_extra_payloads(tmp_path: Path, payloads_kind: str) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    index, snapshot = _ready_index_and_snapshot()
    payloads = {
        **snapshot_to_payloads(snapshot),
        "index_ref.json": dumps_json(index.model_dump(mode="json")).encode("utf-8"),
    }
    if payloads_kind == "missing":
        del payloads["postings.json"]
    else:
        payloads["unexpected.json"] = b"{}"
    metadata = {
        "format": "m2_evidence_index/v1",
        "course_package_id": index.course_package_id,
        "course_package_checksum": index.course_package_checksum,
        "index_checksum": index.checksum,
        "tokenizer_version": snapshot.tokenizer_version,
        "documents_sha256": hashlib.sha256(payloads["documents.json"]).hexdigest(),
        "postings_sha256": hashlib.sha256(payloads.get("postings.json", b"")).hexdigest(),
    }
    runtime = tmp_path / "runtime"
    ImmutableArtifactStore(runtime).publish(
        module="m2", object_type="evidence_index", object_id=index.index_id,
        object_version=index.index_version, payloads=payloads, metadata=metadata,
    )
    _assert_invalid(lambda: FileM2Repository(runtime).load_index_artifact(index.index_id, index.index_version))


def test_file_repository_maps_manifest_tamper_to_safe_invalid(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    runtime = tmp_path / "runtime"
    index, snapshot = _ready_index_and_snapshot()
    FileM2Repository(runtime).save_index_artifact(index, snapshot)
    manifest = next(runtime.rglob("manifest.json"))
    manifest.write_bytes(b"{}")
    _assert_invalid(lambda: FileM2Repository(runtime).load_index_artifact(index.index_id, index.index_version))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda raw: b" " + raw + b"\n",
        lambda raw: raw[:-1] + b',"format_version":1}',
        lambda raw: raw.replace(
            b'"metadata":{"course_package_checksum":',
            b'"metadata":{"course_package_checksum":"duplicate","course_package_checksum":',
        ),
    ],
    ids=["surrounding-whitespace", "duplicate-top-level", "duplicate-nested-metadata"],
)
def test_file_repository_safely_maps_noncanonical_or_duplicate_manifest_json(
    tmp_path: Path, tamper: object
) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    runtime = tmp_path / "runtime"
    index, snapshot = _ready_index_and_snapshot()
    FileM2Repository(runtime).save_index_artifact(index, snapshot)
    manifest = next(runtime.rglob("manifest.json"))
    original = manifest.read_bytes()
    altered = tamper(original)  # type: ignore[operator]
    assert altered != original
    manifest.write_bytes(altered)
    _assert_invalid(
        lambda: FileM2Repository(runtime).load_index_artifact(
            index.index_id, index.index_version
        )
    )


def test_file_repository_two_versions_coexist_and_metadata_tamper_is_safe(tmp_path: Path) -> None:
    from course_insight.infrastructure.m2_file_repository import FileM2Repository

    runtime = tmp_path / "runtime"
    repository = FileM2Repository(runtime)
    first = _ready_index_and_snapshot(version="v1", text="one")
    second = _ready_index_and_snapshot(version="v2", text="two")
    repository.save_index_artifact(*first)
    repository.save_index_artifact(*second)
    assert repository.load_index_artifact(first[0].index_id, "v1") == first
    assert repository.load_index_artifact(second[0].index_id, "v2") == second

    loaded = ImmutableArtifactStore(runtime).load(
        module="m2", object_type="evidence_index", object_id=first[0].index_id, object_version="v1"
    )
    poisoned = tmp_path / "poisoned"
    metadata = dict(loaded.metadata)
    metadata["index_checksum"] = "0" * 64
    ImmutableArtifactStore(poisoned).publish(
        module="m2", object_type="evidence_index", object_id=first[0].index_id,
        object_version="v1", payloads=loaded.payloads, metadata=metadata,
    )
    _assert_invalid(lambda: FileM2Repository(poisoned).load_index_artifact(first[0].index_id, "v1"))
