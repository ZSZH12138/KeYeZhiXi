"""Private, content-addressed artifacts stored below one runtime directory."""

from __future__ import annotations

import hashlib
import json
import os
import errno
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from course_insight.infrastructure.json_io import dumps_json


_FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_PUBLISH_LOCK_NAME = ".publish.lock"
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01


class _FrozenList(tuple[Any, ...]):
    """An immutable JSON list that preserves ordinary list equality."""

    def __eq__(self, other: object) -> bool:
        if type(other) is list:
            return list(self) == other
        return super().__eq__(other)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    relative_dir: Path
    checksum: str


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    ref: ArtifactRef
    payloads: Mapping[str, bytes]
    metadata: Mapping[str, Any]


class ArtifactStoreError(ValueError):
    """A safe artifact-boundary failure identified by a stable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"runtime artifact error: {reason}")


class ImmutableArtifactStore:
    """Publish and load verified immutable byte artifacts below ``runtime_dir``."""

    def __init__(self, runtime_dir: Path) -> None:
        self._runtime_dir = Path(runtime_dir)

    def publish(
        self,
        *,
        module: str,
        object_type: str,
        object_id: str,
        object_version: str,
        payloads: Mapping[str, bytes],
        metadata: Mapping[str, Any],
    ) -> ArtifactRef:
        identity = self._identity(module, object_type, object_id, object_version)
        normalized_payloads = self._payloads(payloads)
        manifest_core = self._manifest_core(identity, normalized_payloads, metadata)
        checksum = self._sha256(self._canonical_bytes(manifest_core))
        manifest = {**manifest_core, "artifact_checksum": checksum}
        reference = ArtifactRef(
            relative_dir=Path("artifacts", *identity, checksum),
            checksum=checksum,
        )

        staging_dir: Path | None = None
        try:
            artifact_root = self._artifact_root()
            identity_dir = self._identity_dir(identity)
            final_dir = self._checked_path(reference.relative_dir)
            artifact_root.mkdir(parents=True, exist_ok=True)
            identity_dir.mkdir(parents=True, exist_ok=True)
            staging_dir = Path(tempfile.mkdtemp(prefix=".staging-", dir=artifact_root))
            self._write_payloads(staging_dir, normalized_payloads)
            self._write_file(staging_dir / _MANIFEST_NAME, self._canonical_bytes(manifest))

            with self._exclusive_publish_lock(identity_dir):
                existing_dirs = self._checksum_dirs(identity_dir)
                if len(existing_dirs) == 1 and existing_dirs[0].name == checksum:
                    self._read_artifact(existing_dirs[0], identity, checksum)
                    return reference
                if existing_dirs:
                    raise ArtifactStoreError("version_conflict")
                try:
                    os.rename(staging_dir, final_dir)
                    staging_dir = None
                except FileExistsError:
                    existing_dirs = self._checksum_dirs(identity_dir)
                    if len(existing_dirs) == 1 and existing_dirs[0].name == checksum:
                        self._read_artifact(existing_dirs[0], identity, checksum)
                        return reference
                    raise ArtifactStoreError("version_conflict") from None
            return reference
        except ArtifactStoreError:
            raise
        except (OSError, TypeError, ValueError):
            raise ArtifactStoreError("artifact_io_error") from None
        finally:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)

    def load(
        self,
        *,
        module: str,
        object_type: str,
        object_id: str,
        object_version: str,
    ) -> LoadedArtifact:
        identity = self._identity(module, object_type, object_id, object_version)
        try:
            directories = self._checksum_dirs(self._identity_dir(identity))
            if not directories:
                raise ArtifactStoreError("missing_artifact")
            if len(directories) != 1:
                raise ArtifactStoreError("multiple_artifacts")
            return self._read_artifact(directories[0], identity, directories[0].name)
        except ArtifactStoreError:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactStoreError("artifact_io_error") from None

    def _identity(
        self,
        module: str,
        object_type: str,
        object_id: str,
        object_version: str,
    ) -> tuple[str, str, str, str]:
        return (
            self._path_segment(module),
            self._path_segment(object_type),
            self._path_segment(object_id),
            self._path_segment(object_version),
        )

    @staticmethod
    def _path_segment(value: str) -> str:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ArtifactStoreError("invalid_identity")
        path = Path(value)
        windows_path = PureWindowsPath(value)
        if (
            path.is_absolute()
            or path.drive
            or windows_path.is_absolute()
            or windows_path.drive
            or len(path.parts) != 1
            or "/" in value
            or "\\" in value
            or value in {".", ".."}
        ):
            raise ArtifactStoreError("invalid_identity")
        return value

    def _payloads(self, payloads: Mapping[str, bytes]) -> dict[str, bytes]:
        if not isinstance(payloads, Mapping):
            raise ArtifactStoreError("invalid_payload")
        normalized: dict[str, bytes] = {}
        for name, contents in payloads.items():
            normalized_name, _ = self._relative_payload_path(name)
            if normalized_name == _MANIFEST_NAME or normalized_name in normalized:
                raise ArtifactStoreError("invalid_payload_path")
            if not isinstance(contents, bytes):
                raise ArtifactStoreError("invalid_payload")
            normalized[normalized_name] = contents
        return normalized

    @staticmethod
    def _relative_payload_path(value: str) -> tuple[str, Path]:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ArtifactStoreError("invalid_payload_path")
        if "\\" in value:
            raise ArtifactStoreError("invalid_payload_path")
        path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        lexical_parts = value.split("/")
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or not path.parts
            or any(part in {".", ".."} for part in lexical_parts)
        ):
            raise ArtifactStoreError("invalid_payload_path")
        return path.as_posix(), Path(*path.parts)

    def _manifest_core(
        self,
        identity: tuple[str, str, str, str],
        payloads: Mapping[str, bytes],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(metadata, Mapping) or any(
            not isinstance(key, str) for key in metadata
        ):
            raise ArtifactStoreError("invalid_metadata")
        try:
            metadata_snapshot = json.loads(dumps_json(dict(metadata)))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactStoreError("invalid_metadata") from None
        manifest = {
            "format_version": _FORMAT_VERSION,
            "identity": {
                "module": identity[0],
                "object_type": identity[1],
                "object_id": identity[2],
                "object_version": identity[3],
            },
            "files": [
                {"path": name, "sha256": self._sha256(contents)}
                for name, contents in sorted(payloads.items())
            ],
            "metadata": metadata_snapshot,
        }
        try:
            self._canonical_bytes(manifest)
        except (TypeError, ValueError):
            raise ArtifactStoreError("invalid_metadata") from None
        return manifest

    def _artifact_root(self) -> Path:
        return self._checked_path(Path("artifacts"))

    def _identity_dir(self, identity: tuple[str, str, str, str]) -> Path:
        return self._checked_path(Path("artifacts", *identity))

    def _checked_path(self, relative_path: Path) -> Path:
        root = self._runtime_dir.resolve()
        path = (root / relative_path).resolve()
        if not path.is_relative_to(root):
            raise ArtifactStoreError("unsafe_reference")
        return path

    def _checksum_dirs(self, identity_dir: Path) -> list[Path]:
        if not identity_dir.exists():
            return []
        self._ensure_contained(identity_dir)
        directories = [path for path in identity_dir.iterdir() if path.is_dir()]
        for directory in directories:
            self._ensure_contained(directory)
        return sorted(directories, key=lambda path: path.name)

    def _ensure_contained(self, path: Path) -> None:
        root = self._runtime_dir.resolve()
        if not path.resolve().is_relative_to(root):
            raise ArtifactStoreError("unsafe_reference")

    @contextmanager
    def _exclusive_publish_lock(self, identity_dir: Path) -> Iterator[None]:
        """Serialize one logical version across processes without path leakage."""

        lock_path = identity_dir / _PUBLISH_LOCK_NAME
        self._ensure_contained(lock_path)
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+b") as lock_file:
            lock_file.seek(0)
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while not self._try_lock(lock_file):
                if time.monotonic() >= deadline:
                    raise ArtifactStoreError("lock_timeout")
                time.sleep(_LOCK_RETRY_SECONDS)
            try:
                yield
            finally:
                self._unlock(lock_file)

    @staticmethod
    def _try_lock(lock_file: Any) -> bool:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                # This runtime maps a lock violation to EACCES without winerror.
                if error.winerror == 33 or (
                    error.winerror is None and error.errno == errno.EACCES
                ):
                    return False
                raise
            return True
        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def _unlock(lock_file: Any) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_payloads(self, staging_dir: Path, payloads: Mapping[str, bytes]) -> None:
        for name, contents in payloads.items():
            target = staging_dir / Path(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._write_file(target, contents)

    @staticmethod
    def _write_file(path: Path, contents: bytes) -> None:
        with path.open("wb") as target:
            target.write(contents)
            target.flush()
            os.fsync(target.fileno())

    def _read_artifact(
        self,
        directory: Path,
        identity: tuple[str, str, str, str],
        checksum: str,
    ) -> LoadedArtifact:
        self._ensure_contained(directory)
        if directory.name != checksum or not self._is_checksum(checksum):
            raise ArtifactStoreError("invalid_manifest")
        manifest_path = directory / _MANIFEST_NAME
        self._ensure_contained(manifest_path)
        try:
            raw_manifest = manifest_path.read_bytes()

            def reject_duplicate_keys(
                pairs: list[tuple[str, Any]],
            ) -> dict[str, Any]:
                parsed: dict[str, Any] = {}
                for key, value in pairs:
                    if key in parsed:
                        raise ValueError("duplicate manifest key")
                    parsed[key] = value
                return parsed

            manifest = json.loads(
                raw_manifest.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
            if (
                type(manifest) is not dict
                or raw_manifest != self._canonical_bytes(manifest)
            ):
                raise ValueError
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            raise ArtifactStoreError("invalid_manifest") from None

        required_fields = {
            "format_version",
            "identity",
            "files",
            "metadata",
            "artifact_checksum",
        }
        if set(manifest) != required_fields:
            raise ArtifactStoreError("invalid_manifest")
        manifest_identity = manifest["identity"]
        identity_fields = {"module", "object_type", "object_id", "object_version"}
        if (
            type(manifest["format_version"]) is not int
            or type(manifest_identity) is not dict
            or set(manifest_identity) != identity_fields
            or any(type(value) is not str for value in manifest_identity.values())
            or type(manifest["files"]) is not list
            or type(manifest["metadata"]) is not dict
        ):
            raise ArtifactStoreError("invalid_manifest")
        actual_checksum = manifest.get("artifact_checksum")
        core = dict(manifest)
        core.pop("artifact_checksum", None)
        try:
            calculated_checksum = self._sha256(self._canonical_bytes(core))
        except (TypeError, ValueError):
            raise ArtifactStoreError("invalid_manifest") from None
        if (
            not isinstance(actual_checksum, str)
            or actual_checksum != checksum
            or calculated_checksum != checksum
        ):
            raise ArtifactStoreError("checksum_mismatch")
        expected_identity = {
            "module": identity[0],
            "object_type": identity[1],
            "object_id": identity[2],
            "object_version": identity[3],
        }
        if (
            manifest.get("format_version") != _FORMAT_VERSION
            or manifest.get("identity") != expected_identity
        ):
            raise ArtifactStoreError("identity_mismatch")
        metadata = manifest.get("metadata")
        files = manifest.get("files")
        if type(metadata) is not dict or type(files) is not list:
            raise ArtifactStoreError("invalid_manifest")

        payloads: dict[str, bytes] = {}
        expected_files: list[dict[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ArtifactStoreError("invalid_manifest")
            name, file_checksum = item["path"], item["sha256"]
            try:
                normalized_name, safe_path = self._relative_payload_path(name)
            except ArtifactStoreError:
                raise ArtifactStoreError("invalid_manifest") from None
            if (
                not isinstance(file_checksum, str)
                or not self._is_checksum(file_checksum)
                or normalized_name in payloads
            ):
                raise ArtifactStoreError("invalid_manifest")
            payload_path = directory / safe_path
            self._ensure_contained(payload_path)
            try:
                contents = payload_path.read_bytes()
            except OSError:
                raise ArtifactStoreError("payload_mismatch") from None
            if self._sha256(contents) != file_checksum:
                raise ArtifactStoreError("payload_mismatch")
            payloads[normalized_name] = contents
            expected_files.append({"path": normalized_name, "sha256": file_checksum})
        if files != sorted(expected_files, key=lambda item: item["path"]):
            raise ArtifactStoreError("invalid_manifest")
        actual_files: list[str] = []
        for path in directory.rglob("*"):
            self._ensure_contained(path)
            if path.is_file():
                actual_files.append(path.relative_to(directory).as_posix())
        if sorted(actual_files) != sorted([_MANIFEST_NAME, *payloads]):
            raise ArtifactStoreError("payload_mismatch")
        return LoadedArtifact(
            ref=ArtifactRef(
                relative_dir=Path("artifacts", *identity, checksum),
                checksum=checksum,
            ),
            payloads=MappingProxyType(payloads),
            metadata=MappingProxyType(
                {key: self._freeze_json(value) for key, value in metadata.items()}
            ),
        )

    @classmethod
    def _freeze_json(cls, value: Any) -> Any:
        if type(value) is dict:
            return MappingProxyType(
                {key: cls._freeze_json(item) for key, item in value.items()}
            )
        if type(value) is list:
            return _FrozenList(cls._freeze_json(item) for item in value)
        return value

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return dumps_json(value).encode("utf-8")

    @staticmethod
    def _is_checksum(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    @staticmethod
    def _sha256(contents: bytes) -> str:
        return hashlib.sha256(contents).hexdigest()
