import hashlib
import json
import multiprocessing
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

import course_insight.infrastructure.runtime_artifacts as runtime_artifacts
from course_insight.infrastructure.runtime_artifacts import (
    ArtifactStoreError,
    ImmutableArtifactStore,
)
from course_insight.infrastructure.json_io import dumps_json


def _publish_after_staging_barrier(
    runtime_dir: str,
    barrier: Any,
    start: Any,
    ready: Any,
    payload: bytes,
    results: Any,
) -> None:
    store = ImmutableArtifactStore(Path(runtime_dir))
    ready.put("ready")
    if not start.wait(timeout=20):
        results.put("unexpected:start_timeout")
        return
    original_write_file = ImmutableArtifactStore._write_file

    def synchronized_write_file(path: Path, contents: bytes) -> None:
        original_write_file(path, contents)
        if path.name == "manifest.json":
            barrier.wait(timeout=20)

    ImmutableArtifactStore._write_file = staticmethod(synchronized_write_file)
    try:
        store.publish(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={"course_package.json": payload},
            metadata={"course_id": "course_1"},
        )
    except ArtifactStoreError as error:
        results.put(error.reason)
    except Exception as error:
        results.put(f"unexpected:{type(error).__name__}")
    else:
        results.put("success")
    finally:
        ImmutableArtifactStore._write_file = staticmethod(original_write_file)


def _hold_identity_publish_lock(runtime_dir: str, ready: Any) -> None:
    store = ImmutableArtifactStore(Path(runtime_dir))
    identity_dir = Path(runtime_dir) / "artifacts/m1/course_package/package_1/1.0.0"
    identity_dir.mkdir(parents=True, exist_ok=True)
    with store._exclusive_publish_lock(identity_dir):
        ready.put("locked")
        time.sleep(30)


def test_publish_and_load_round_trip_returns_verified_bytes(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")

    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )

    loaded = store.load(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
    )

    assert reference.relative_dir == Path(
        "artifacts/m1/course_package/package_1/1.0.0"
    ) / reference.checksum
    assert not reference.relative_dir.is_absolute()
    assert loaded.ref == reference
    assert loaded.payloads == {"course_package.json": b"{}\n"}
    assert loaded.metadata == {"course_id": "course_1"}


def test_metadata_is_deep_snapshotted_and_loaded_immutably(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    metadata = {"nested": {"values": ["initial"]}}
    store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata=metadata,
    )
    metadata["nested"]["values"].append("caller-mutation")

    loaded = store.load(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
    )

    assert loaded.metadata == {"nested": {"values": ["initial"]}}
    with pytest.raises(TypeError):
        loaded.metadata["nested"]["values"] = ()
    with pytest.raises(AttributeError):
        loaded.metadata["nested"]["values"].append("loaded-mutation")


def test_load_rejects_payloads_not_listed_in_the_manifest(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    artifact_dir = tmp_path / "runtime" / reference.relative_dir
    (artifact_dir / "unexpected.bin").write_bytes(b"not accounted for")

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "payload_mismatch"


def test_publish_identical_content_is_idempotent(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    request = {
        "module": "m1",
        "object_type": "course_package",
        "object_id": "package_1",
        "object_version": "1.0.0",
        "payloads": {"course_package.json": b"{}\n"},
        "metadata": {"course_id": "course_1"},
    }

    first = store.publish(**request)
    second = store.publish(**request)

    assert second == first
    version_dir = tmp_path / "runtime" / first.relative_dir.parent
    assert len([path for path in version_dir.iterdir() if path.is_dir()]) == 1


def test_publish_same_identity_with_different_content_conflicts(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    identity = {
        "module": "m1",
        "object_type": "course_package",
        "object_id": "package_1",
        "object_version": "1.0.0",
        "metadata": {"course_id": "course_1"},
    }
    store.publish(**identity, payloads={"course_package.json": b"{}\n"})

    with pytest.raises(ArtifactStoreError) as captured:
        store.publish(**identity, payloads={"course_package.json": b'{"changed":true}\n'})

    assert captured.value.reason == "version_conflict"


def test_concurrent_publish_of_different_content_conflicts_after_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    barrier = Barrier(2)
    original_write_file = ImmutableArtifactStore._write_file

    def synchronized_write_file(path: Path, contents: bytes) -> None:
        original_write_file(path, contents)
        if path.name == "manifest.json":
            barrier.wait(timeout=60)

    monkeypatch.setattr(
        ImmutableArtifactStore,
        "_write_file",
        staticmethod(synchronized_write_file),
    )

    def publish(payload: bytes) -> str:
        try:
            store.publish(
                module="m1",
                object_type="course_package",
                object_id="package_1",
                object_version="1.0.0",
                payloads={"course_package.json": payload},
                metadata={"course_id": "course_1"},
            )
        except ArtifactStoreError as error:
            return error.reason
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, [b'{"one":true}\n', b'{"two":true}\n']))

    version_dir = tmp_path / "runtime" / "artifacts/m1/course_package/package_1/1.0.0"
    checksum_dirs = [path for path in version_dir.iterdir() if path.is_dir()]
    assert results.count("success") == 1
    assert results.count("version_conflict") == 1
    assert len(checksum_dirs) == 1


def test_processes_publish_different_content_with_one_version_winner(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    start = context.Event()
    ready = context.Queue()
    results = context.Queue()
    runtime_dir = tmp_path / "runtime"
    processes = [
        context.Process(
            target=_publish_after_staging_barrier,
            args=(str(runtime_dir), barrier, start, ready, payload, results),
        )
        for payload in (b'{"one":true}\n', b'{"two":true}\n')
    ]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=10), ready.get(timeout=10)] == ["ready", "ready"]
        start.set()
        deadline = time.monotonic() + 30
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))

        assert [process.exitcode for process in processes] == [0, 0]
        outcomes = sorted([results.get(timeout=10), results.get(timeout=10)])
        version_dir = runtime_dir / "artifacts/m1/course_package/package_1/1.0.0"
        checksum_dirs = [path for path in version_dir.iterdir() if path.is_dir()]
        assert outcomes == ["success", "version_conflict"]
        assert len(checksum_dirs) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()
        ready.close()
        ready.join_thread()


def test_terminated_lock_holder_releases_lock_for_later_publish(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    runtime_dir = tmp_path / "runtime"
    holder = context.Process(
        target=_hold_identity_publish_lock,
        args=(str(runtime_dir), ready),
    )
    holder.start()
    try:
        assert ready.get(timeout=10) == "locked"
        holder.terminate()
        holder.join(timeout=10)
        assert not holder.is_alive()

        reference = ImmutableArtifactStore(runtime_dir).publish(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={"course_package.json": b"{}\n"},
            metadata={"course_id": "course_1"},
        )

        assert (runtime_dir / reference.relative_dir).is_dir()
    finally:
        if holder.is_alive():
            holder.terminate()
        holder.join(timeout=5)
        ready.close()
        ready.join_thread()


def test_concurrent_publish_of_identical_content_is_idempotent_after_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    barrier = Barrier(2)
    original_write_file = ImmutableArtifactStore._write_file

    def synchronized_write_file(path: Path, contents: bytes) -> None:
        original_write_file(path, contents)
        if path.name == "manifest.json":
            barrier.wait(timeout=60)

    monkeypatch.setattr(
        ImmutableArtifactStore,
        "_write_file",
        staticmethod(synchronized_write_file),
    )

    def publish() -> str:
        try:
            store.publish(
                module="m1",
                object_type="course_package",
                object_id="package_1",
                object_version="1.0.0",
                payloads={"course_package.json": b'{"same":true}\n'},
                metadata={"course_id": "course_1"},
            )
        except ArtifactStoreError as error:
            return error.reason
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish(), range(2)))

    version_dir = tmp_path / "runtime" / "artifacts/m1/course_package/package_1/1.0.0"
    checksum_dirs = [path for path in version_dir.iterdir() if path.is_dir()]
    assert results == ["success", "success"]
    assert len(checksum_dirs) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows lock classification")
def test_windows_eacces_without_winerror_is_lock_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msvcrt

    def contended_lock(*_: object) -> None:
        raise PermissionError(13, "permission denied")

    lock_path = tmp_path / "lock"
    lock_path.write_bytes(b"\0")
    monkeypatch.setattr(msvcrt, "locking", contended_lock)
    with lock_path.open("r+b") as lock_file:
        assert ImmutableArtifactStore(tmp_path)._try_lock(lock_file) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows lock classification")
def test_publish_reports_non_contention_windows_lock_failure_as_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msvcrt

    def deny_lock(*_: object) -> None:
        raise OSError(5, "access denied", None, 5)

    monkeypatch.setattr(runtime_artifacts, "_LOCK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(msvcrt, "locking", deny_lock)
    store = ImmutableArtifactStore(tmp_path / "runtime")

    with pytest.raises(ArtifactStoreError) as captured:
        store.publish(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={"course_package.json": b"{}\n"},
            metadata={"course_id": "course_1"},
        )

    assert captured.value.reason == "artifact_io_error"


@pytest.mark.parametrize("unsafe_module", ["../m1", "C:/outside", "m1/"])
def test_publish_rejects_absolute_and_traversal_identity_segments(
    tmp_path: Path,
    unsafe_module: str,
) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")

    with pytest.raises(ArtifactStoreError) as captured:
        store.publish(
            module=unsafe_module,
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={"course_package.json": b"{}\n"},
            metadata={"course_id": "course_1"},
        )

    assert captured.value.reason == "invalid_identity"
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize(
    "unsafe_payload",
    ["../payload.bin", "C:/outside.bin", "nested/./payload.bin"],
)
def test_publish_rejects_absolute_and_traversal_payload_paths(
    tmp_path: Path,
    unsafe_payload: str,
) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")

    with pytest.raises(ArtifactStoreError) as captured:
        store.publish(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={unsafe_payload: b"{}\n"},
            metadata={"course_id": "course_1"},
        )

    assert captured.value.reason == "invalid_payload_path"
    assert not (tmp_path / "runtime").exists()


def test_payload_wire_paths_are_posix_and_reject_backslashes(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")

    with pytest.raises(ArtifactStoreError) as captured:
        store.publish(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={"nested\\artifact.json": b"{}\n"},
            metadata={"course_id": "course_1"},
        )

    assert captured.value.reason == "invalid_payload_path"
    with pytest.raises(ArtifactStoreError) as captured:
        store.publish(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
            payloads={
                "nested//artifact.json": b"first\n",
                "nested/artifact.json": b"second\n",
            },
            metadata={"course_id": "course_1"},
        )

    assert captured.value.reason == "invalid_payload_path"
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"nested/artifact.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    loaded = store.load(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
    )
    manifest = json.loads(
        (tmp_path / "runtime" / reference.relative_dir / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert loaded.payloads == {"nested/artifact.json": b"{}\n"}
    assert manifest["files"][0]["path"] == "nested/artifact.json"


def test_load_rejects_tampered_payload(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    (tmp_path / "runtime" / reference.relative_dir / "course_package.json").write_bytes(
        b'{"tampered":true}\n'
    )

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "payload_mismatch"


def test_load_rejects_tampered_manifest(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    manifest_path = tmp_path / "runtime" / reference.relative_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"]["course_id"] = "course_2"
    manifest_path.write_text(dumps_json(manifest), encoding="utf-8")

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "checksum_mismatch"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda raw: b" " + raw + b"\n",
        lambda raw: raw[:-1] + b',"format_version":1}',
        lambda raw: raw.replace(
            b'"metadata":{"course_id":"course_1"}',
            b'"metadata":{"course_id":"course_1","course_id":"course_1"}',
        ),
    ],
    ids=["surrounding-whitespace", "duplicate-top-level", "duplicate-nested-metadata"],
)
def test_load_rejects_noncanonical_or_duplicate_manifest_json(
    tmp_path: Path, tamper: Any
) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    manifest_path = tmp_path / "runtime" / reference.relative_dir / "manifest.json"
    original = manifest_path.read_bytes()
    altered = tamper(original)
    assert altered != original
    manifest_path.write_bytes(altered)

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "invalid_manifest"


def test_load_rejects_manifest_with_unknown_top_level_field(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    artifact_dir = tmp_path / "runtime" / reference.relative_dir
    manifest_path = artifact_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = "field"
    manifest_core = {
        key: value for key, value in manifest.items() if key != "artifact_checksum"
    }
    checksum = hashlib.sha256(dumps_json(manifest_core).encode("utf-8")).hexdigest()
    manifest["artifact_checksum"] = checksum
    manifest_path.write_text(dumps_json(manifest), encoding="utf-8")
    artifact_dir.rename(artifact_dir.parent / checksum)

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "invalid_manifest"


def test_load_rejects_missing_artifact(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "missing_artifact"


def test_load_rejects_multiple_artifact_directories(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )
    artifact_dir = tmp_path / "runtime" / reference.relative_dir
    shutil.copytree(artifact_dir, artifact_dir.parent / ("a" * 64))

    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="package_1",
            object_version="1.0.0",
        )

    assert captured.value.reason == "multiple_artifacts"


def test_errors_and_refs_do_not_expose_absolute_paths(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "runtime")
    reference = store.publish(
        module="m1",
        object_type="course_package",
        object_id="package_1",
        object_version="1.0.0",
        payloads={"course_package.json": b"{}\n"},
        metadata={"course_id": "course_1"},
    )

    assert not reference.relative_dir.is_absolute()
    assert str(tmp_path) not in str(reference)
    with pytest.raises(ArtifactStoreError) as captured:
        store.load(
            module="m1",
            object_type="course_package",
            object_id="absent",
            object_version="1.0.0",
        )
    assert str(captured.value) == "runtime artifact error: missing_artifact"
    assert str(tmp_path) not in str(captured.value)
