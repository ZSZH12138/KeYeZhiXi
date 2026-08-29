"""Private immutable-file persistence for complete M1 imports."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.runtime_artifacts import (
    ArtifactStoreError,
    ImmutableArtifactStore,
)
from course_insight.modules.m1_course_governance.authorization import parse_authorizations
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)


_MODULE = "m1"
_OBJECT_TYPE = "course_package"
_FORMAT = "m1_course_package/v1"
_REQUIRED_PAYLOADS = frozenset({
    "course_package.json", "parse_failures.json", "inputs/course_metadata.json",
    "inputs/source_authorization.csv",
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _invalid() -> DomainError:
    return DomainError(
        code="COURSE_ARTIFACT_INVALID", module="m1",
        message="course package artifact is invalid",
    )


def _version_conflict() -> DomainError:
    return DomainError(
        code="COURSE_VERSION_CONFLICT", module="m1",
        message="course package version conflicts with an immutable artifact",
    )


class FileM1Repository:
    """Persist only complete, checksum-bound M1 imports under ``runtime``."""

    def __init__(self, runtime_dir: Path) -> None:
        self._store = ImmutableArtifactStore(runtime_dir)

    def save_course_package(self, package: CoursePackage) -> None:
        del package
        raise _invalid()

    def save_course_import(
        self, package: CoursePackage, snapshot: CourseImportSnapshot
    ) -> None:
        try:
            payloads, metadata = self._serialize(package, snapshot)
            self._store.publish(
                module=_MODULE, object_type=_OBJECT_TYPE,
                object_id=package.course_package_id,
                object_version=package.package_version,
                payloads=payloads, metadata=metadata,
            )
        except DomainError:
            raise
        except ArtifactStoreError as error:
            if error.reason == "version_conflict":
                raise _version_conflict() from None
            raise _invalid() from None
        except Exception:
            raise _invalid() from None

    def get_course_package(
        self, course_package_id: str, package_version: str
    ) -> CoursePackage | None:
        try:
            loaded = self._store.load(
                module=_MODULE, object_type=_OBJECT_TYPE,
                object_id=course_package_id, object_version=package_version,
            )
        except ArtifactStoreError as error:
            if error.reason == "missing_artifact":
                return None
            raise _invalid() from None
        try:
            return self._deserialize(
                loaded.payloads, loaded.metadata,
                course_package_id, package_version,
            )
        except Exception:
            raise _invalid() from None

    @staticmethod
    def _valid_package(package: CoursePackage) -> None:
        if package.status != "ready" or package.checksum != package.recalculate_checksum():
            raise _invalid()
        if package.imported_at.tzinfo is None or package.imported_at.utcoffset() is None:
            raise _invalid()
        source_ids = [source.source_id for source in package.source_documents]
        authorization_ids = [item.source_id for item in package.source_authorizations]
        if (
            not source_ids
            or len(source_ids) != len(set(source_ids))
            or len(package.source_authorizations) != len(package.source_documents)
            or len(authorization_ids) != len(set(authorization_ids))
            or set(authorization_ids) != set(source_ids)
            or any(
                source.title != package.source_documents[0].title
                or source.version != package.package_version
                for source in package.source_documents
            )
        ):
            raise _invalid()
        for chunk in package.content_chunks:
            canonical_text = "\n".join(
                unicodedata.normalize("NFKC", line).rstrip()
                for line in chunk.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            ).strip()
            text_sha = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            expected_chunk_id = "chunk_" + hashlib.sha256(
                f"{chunk.source_id}\0{chunk.locator}\0{text_sha}".encode("utf-8")
            ).hexdigest()
            if (
                not isinstance(chunk.locator, str)
                or not chunk.locator or chunk.locator != chunk.locator.strip()
                or "\x00" in chunk.locator
                or chunk.text != canonical_text
                or not _SHA256_RE.fullmatch(chunk.sha256)
                or chunk.sha256 != text_sha
                or chunk.chunk_id != expected_chunk_id
            ):
                raise _invalid()

    def _serialize(
        self, package: CoursePackage, snapshot: CourseImportSnapshot
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        self._valid_package(package)
        if not isinstance(snapshot, CourseImportSnapshot):
            raise _invalid()
        if (
            type(snapshot.course_metadata_bytes) is not bytes
            or type(snapshot.source_authorization_bytes) is not bytes
            or type(snapshot.source_payloads) is not tuple
        ):
            raise _invalid()
        for payload in snapshot.source_payloads:
            if (
                type(payload) is not SourcePayload
                or type(payload.source_id) is not str
                or type(payload.file_name) is not str
                or type(payload.raw_bytes) is not bytes
            ):
                raise _invalid()
        try:
            course_metadata = self._read_json(snapshot.course_metadata_bytes)
            authorization_rows = parse_authorizations(snapshot.source_authorization_bytes)
        except DomainError:
            raise _invalid() from None
        sources = {source.source_id: source for source in package.source_documents}
        source_payloads = {payload.source_id: payload for payload in snapshot.source_payloads}
        if set(sources) != set(source_payloads) or len(source_payloads) != len(snapshot.source_payloads):
            raise _invalid()
        if set(authorization_rows) != {source.file_name for source in sources.values()}:
            raise _invalid()
        self._validate_metadata(course_metadata, package)
        payloads: dict[str, bytes] = {
            "course_package.json": dumps_json(package.model_dump(mode="json")).encode("utf-8"),
            "parse_failures.json": b"[]",
            "inputs/course_metadata.json": snapshot.course_metadata_bytes,
            "inputs/source_authorization.csv": snapshot.source_authorization_bytes,
        }
        metadata_sources: list[dict[str, str]] = []
        authorizations = {item.source_id: item for item in package.source_authorizations}
        if set(authorizations) != set(sources):
            raise _invalid()
        for source_id, source in sorted(sources.items()):
            payload = source_payloads[source_id]
            row = authorization_rows.get(source.file_name)
            authorization = authorizations[source_id]
            if (
                payload.file_name != source.file_name
                or self._sha(payload.raw_bytes) != source.sha256
                or row is None or row.source_id != source_id
                or row.authorized_by != authorization.authorized_by
                or row.license_note != authorization.license_note
                or not self._authorization_time_matches(
                    row.authorized_at, authorization.authorized_at
                )
            ):
                raise _invalid()
            expected = row.expected_sha256
            if row.legacy_hash:
                expected = course_metadata.get("source_sha256") if len(sources) == 1 else ""
            if not isinstance(expected, str) or not hmac.compare_digest(expected, source.sha256):
                raise _invalid()
            source_path = f"sources/{source_id}/{source.file_name}"
            payloads[source_path] = payload.raw_bytes
            metadata_sources.append({
                "source_id": source_id, "file_name": source.file_name,
                "sha256": source.sha256, "payload_path": source_path,
            })
        return payloads, {
            "format": _FORMAT, "course_id": package.course_id,
            "package_checksum": package.checksum, "sources": metadata_sources,
        }

    def _deserialize(
        self,
        payloads: Mapping[str, bytes], metadata: Mapping[str, Any],
        course_package_id: str, package_version: str,
    ) -> CoursePackage:
        if not isinstance(payloads, Mapping) or not isinstance(metadata, Mapping):
            raise _invalid()
        required = set(_REQUIRED_PAYLOADS)
        try:
            package_data = self._read_json(payloads["course_package.json"])
            failures = self._read_json(payloads["parse_failures.json"])
            course_metadata = self._read_json(payloads["inputs/course_metadata.json"])
            authorization_rows = parse_authorizations(payloads["inputs/source_authorization.csv"])
        except (KeyError, DomainError):
            raise _invalid() from None
        if failures != []:
            raise _invalid()
        package = CoursePackage.model_validate(package_data)
        if (
            package.course_package_id != course_package_id
            or package.package_version != package_version
            or package.status != "ready"
            or package.checksum != package.recalculate_checksum()
        ):
            raise _invalid()
        self._valid_package(package)
        self._validate_metadata(course_metadata, package)
        expected_metadata = self._artifact_metadata(package)
        if dict(metadata) != expected_metadata:
            raise _invalid()
        source_paths = {item["payload_path"] for item in expected_metadata["sources"]}
        if set(payloads) != required | source_paths:
            raise _invalid()
        authorizations = {item.source_id: item for item in package.source_authorizations}
        if set(authorizations) != {source.source_id for source in package.source_documents}:
            raise _invalid()
        if set(authorization_rows) != {source.file_name for source in package.source_documents}:
            raise _invalid()
        for source in package.source_documents:
            path = f"sources/{source.source_id}/{source.file_name}"
            raw = payloads[path]
            row = authorization_rows[source.file_name]
            authorization = authorizations[source.source_id]
            if (
                self._sha(raw) != source.sha256 or row.source_id != source.source_id
                or row.authorized_by != authorization.authorized_by
                or row.license_note != authorization.license_note
                or not self._authorization_time_matches(
                    row.authorized_at, authorization.authorized_at
                )
            ):
                raise _invalid()
            expected = row.expected_sha256
            if row.legacy_hash:
                expected = course_metadata.get("source_sha256") if len(package.source_documents) == 1 else ""
            if not isinstance(expected, str) or not hmac.compare_digest(expected, source.sha256):
                raise _invalid()
        return package

    @staticmethod
    def _read_json(payload: bytes) -> dict[str, Any] | list[Any]:
        value = json.loads(payload.decode("utf-8"))
        if type(value) not in {dict, list}:
            raise ValueError("artifact JSON root must be a container")
        return value

    @staticmethod
    def _validate_metadata(metadata: object, package: CoursePackage) -> None:
        if not isinstance(metadata, dict):
            raise _invalid()
        expected = {
            "course_package_id": package.course_package_id,
            "course_id": package.course_id,
            "package_version": package.package_version,
            "course_name": package.source_documents[0].title if package.source_documents else None,
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise _invalid()
        if any(
            source.title != metadata["course_name"]
            or source.version != package.package_version
            for source in package.source_documents
        ):
            raise _invalid()
        imported_at = metadata.get("imported_at")
        if not isinstance(imported_at, str):
            raise _invalid()
        try:
            parsed = datetime.fromisoformat(imported_at.replace("Z", "+00:00"))
        except ValueError:
            raise _invalid() from None
        if (
            parsed.tzinfo is None or parsed.utcoffset() is None
            or package.imported_at.tzinfo is None or package.imported_at.utcoffset() is None
            or parsed != package.imported_at
        ):
            raise _invalid()

    @staticmethod
    def _artifact_metadata(package: CoursePackage) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "course_id": package.course_id,
            "package_checksum": package.checksum,
            "sources": [
                {
                    "source_id": source.source_id, "file_name": source.file_name,
                    "sha256": source.sha256,
                    "payload_path": f"sources/{source.source_id}/{source.file_name}",
                }
                for source in sorted(package.source_documents, key=lambda item: item.source_id)
            ],
        }

    @staticmethod
    def _sha(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _authorization_time_matches(raw_value: str, expected: datetime) -> bool:
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00")) == expected
        except ValueError:
            return False
