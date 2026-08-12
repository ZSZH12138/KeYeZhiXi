"""Authoritative SQLite repository for complete M1-M3 S1-S6 artifacts.

The repository stores one canonical JSON envelope per immutable logical
identity.  Contract-specific serialization and validation remain delegated to
the existing file repositories, so the database adapter cannot accidentally
turn a metadata-only row into a restorable artifact.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from course_insight.application.persistence import BackendReadiness
from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.m1_file_repository import FileM1Repository
from course_insight.infrastructure.m2_file_repository import FileM2Repository
from course_insight.infrastructure.m3_file_repository import FileM3Repository
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.infrastructure.sqlite.migrations_s1_s6 import (
    S1_S6_SCHEMA_VERSION,
    current_schema_version as current_s1_s6_schema_version,
    validate_schema,
)
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    RetrievalAuditEnvelope,
)
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationReport,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    TeacherReviewRecord,
)


_TABLE = "s1_s6_artifacts"
_PAYLOAD_VERSION = "s1_s6/v1"
_PAYLOAD_FORMAT = "s1_s6_payload/v1"
_MANIFEST_FORMAT = "s1_s6_repository_manifest/v1"
_RECORD_KEYS = frozenset(
    {
        "module",
        "object_type",
        "object_id",
        "object_version",
        "status",
        "content_checksum",
        "payload_version",
        "payload_checksum",
        "payload",
    }
)
_PAYLOAD_KEYS = frozenset({"format", "metadata", "payloads"})
_SHA256_ALPHABET = frozenset("0123456789abcdef")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _invalid(module: str, code: str, message: str) -> DomainError:
    return DomainError(code=code, module=module, message=message)


def _persistence_invalid() -> DomainError:
    return _invalid(
        "persistence",
        "PERSISTENCE_ARTIFACT_INVALID",
        "SQLite persistence artifact is invalid",
    )


def _conflict() -> DomainError:
    return DomainError(
        code="PERSISTENCE_VERSION_CONFLICT",
        module="persistence",
        message="immutable persistence identity conflicts with an existing record",
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= _SHA256_ALPHABET
    )


def _strict_json_object(payload: bytes, *, name: str) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise ValueError(f"{name} must be bytes")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError(f"{name} is not valid JSON") from None
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    if dumps_json(value).encode("utf-8") != payload:
        raise ValueError(f"{name} is not canonical JSON")
    return value


def _safe_payload_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value.startswith(("/", "\\")) or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _encode_payload_envelope(
    payloads: Mapping[str, bytes], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(payloads, Mapping) or not payloads:
        raise ValueError("payload set is invalid")
    if not isinstance(metadata, Mapping):
        raise ValueError("artifact metadata is invalid")
    entries: list[dict[str, str]] = []
    for path, payload in sorted(payloads.items()):
        if not _safe_payload_path(path) or type(payload) is not bytes:
            raise ValueError("artifact payload is invalid")
        entries.append(
            {
                "base64": base64.b64encode(payload).decode("ascii"),
                "path": path,
            }
        )
    envelope = {
        "format": _PAYLOAD_FORMAT,
        "metadata": dict(metadata),
        "payloads": entries,
    }
    # Force validation of the complete JSON tree before it reaches SQLite.
    dumps_json(envelope)
    return envelope


def _decode_payload_envelope(
    envelope: object,
    *,
    canonical_json: bytes | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    if type(envelope) is not dict or set(envelope) != _PAYLOAD_KEYS:
        raise ValueError("payload envelope keys are invalid")
    if envelope["format"] != _PAYLOAD_FORMAT:
        raise ValueError("payload envelope format is invalid")
    if type(envelope["metadata"]) is not dict:
        raise ValueError("payload envelope metadata is invalid")
    entries = envelope["payloads"]
    if type(entries) is not list or not entries:
        raise ValueError("payload envelope entries are invalid")
    payloads: dict[str, bytes] = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {"base64", "path"}:
            raise ValueError("payload envelope entry is invalid")
        path, encoded = entry["path"], entry["base64"]
        if not _safe_payload_path(path) or type(encoded) is not str:
            raise ValueError("payload envelope path is invalid")
        if path in payloads:
            raise ValueError("payload envelope contains duplicate paths")
        try:
            payload = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, binascii.Error, UnicodeEncodeError):
            raise ValueError("payload envelope encoding is invalid") from None
        payloads[path] = payload
    if canonical_json is not None and dumps_json(envelope).encode("utf-8") != canonical_json:
        raise ValueError("payload envelope is not canonical JSON")
    return payloads, dict(envelope["metadata"])


def _m1_codec() -> FileM1Repository:
    # The existing private codec methods are pure serializer/validator methods;
    # constructing the object without a store avoids creating a second backend.
    return object.__new__(FileM1Repository)


def _m2_codec() -> FileM2Repository:
    return object.__new__(FileM2Repository)


def _m1_snapshot(
    package: CoursePackage, payloads: Mapping[str, bytes]
) -> CourseImportSnapshot:
    try:
        source_payloads = tuple(
            SourcePayload(
                source_id=source.source_id,
                file_name=source.file_name,
                raw_bytes=payloads[
                    f"sources/{source.source_id}/{source.file_name}"
                ],
            )
            for source in package.source_documents
        )
        return CourseImportSnapshot(
            course_metadata_bytes=payloads["inputs/course_metadata.json"],
            source_authorization_bytes=payloads[
                "inputs/source_authorization.csv"
            ],
            source_payloads=source_payloads,
        )
    except (KeyError, TypeError):
        raise ValueError("M1 snapshot payload is incomplete") from None


def _record(
    *,
    module: str,
    object_type: str,
    object_id: str,
    object_version: str,
    status: str,
    content_checksum: str,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    payload_json = dumps_json(dict(envelope)).encode("utf-8")
    return {
        "module": module,
        "object_type": object_type,
        "object_id": object_id,
        "object_version": object_version,
        "status": status,
        "content_checksum": content_checksum,
        "payload_version": _PAYLOAD_VERSION,
        "payload_checksum": _sha256(payload_json),
        "payload": dict(envelope),
    }


def _validate_record_semantics(record: Mapping[str, Any]) -> None:
    """Validate the complete contract behind a portable record envelope."""

    payloads, metadata = _decode_payload_envelope(record["payload"])
    module = record["module"]
    object_type = record["object_type"]
    if module == "m1" and object_type == "course_import":
        codec = _m1_codec()
        package = codec._deserialize(
            payloads,
            metadata,
            record["object_id"],
            record["object_version"],
        )
        _m1_snapshot(package, payloads)
        if (
            package.status != record["status"]
            or package.checksum != record["content_checksum"]
        ):
            raise ValueError("M1 record bindings differ")
        return
    if module == "m2" and object_type == "evidence_index":
        codec = _m2_codec()
        index, _snapshot = codec._deserialize(
            payloads,
            metadata,
            record["object_id"],
            record["object_version"],
        )
        if (
            index.status != record["status"]
            or index.checksum != record["content_checksum"]
        ):
            raise ValueError("M2 record bindings differ")
        return
    if module == "m2" and object_type == "retrieval_audit":
        envelope = RetrievalAuditEnvelope.from_payload_text(
            payloads["audit.json"] if "audit.json" in payloads else b""
        )
        payload_json = envelope.to_payload_text().encode("utf-8")
        if (
            envelope.audit.audit_id != record["object_id"]
            or envelope.audit.status != record["status"]
            or _sha256(payload_json) != record["content_checksum"]
        ):
            raise ValueError("retrieval audit bindings differ")
        return
    if module == "m3" and object_type == "knowledge_bundle":
        bundle, _report, _snapshot = FileM3Repository._decode_approved(
            payloads,
            metadata,
            record["object_id"],
            record["object_version"],
        )
        if (
            bundle.status != record["status"]
            or bundle.content_checksum() != record["content_checksum"]
        ):
            raise ValueError("M3 record bindings differ")
        return
    if module == "m3" and object_type == "knowledge_validation":
        report, _snapshot = FileM3Repository._decode_rejected(
            payloads,
            metadata,
            record["object_id"],
            record["object_version"],
        )
        if (
            report.status != record["status"]
            or report.checksum != record["content_checksum"]
        ):
            raise ValueError("M3 validation bindings differ")
        return
    if module == "m3" and object_type == "teacher_review":
        review = TeacherReviewRecord.from_payload_text(
            payloads["review.json"] if "review.json" in payloads else b""
        )
        if (
            review.review_id != record["object_id"]
            or str(review.version) != record["object_version"]
            or review.state != record["status"]
            or _sha256(review.to_payload_text().encode("utf-8"))
            != record["content_checksum"]
        ):
            raise ValueError("teacher review bindings differ")
        return
    raise ValueError("repository record type is unsupported")


def _validate_record(record: object) -> dict[str, Any]:
    if type(record) is not dict or set(record) != _RECORD_KEYS:
        raise ValueError("repository record keys are invalid")
    for key in (
        "module",
        "object_type",
        "object_id",
        "object_version",
        "status",
        "payload_version",
    ):
        if type(record[key]) is not str or not record[key].strip():
            raise ValueError("repository record identity is invalid")
    if record["module"] not in {"m1", "m2", "m3"}:
        raise ValueError("repository record module is invalid")
    if not _is_sha256(record["content_checksum"]):
        raise ValueError("repository content checksum is invalid")
    if record["payload_version"] != _PAYLOAD_VERSION:
        raise ValueError("repository payload version is invalid")
    envelope = record["payload"]
    _decode_payload_envelope(envelope)
    payload_json = dumps_json(envelope).encode("utf-8")
    if record["payload_checksum"] != _sha256(payload_json):
        raise ValueError("repository payload checksum is invalid")
    if type(record["status"]) is not str or not record["status"]:
        raise ValueError("repository status is invalid")
    try:
        _validate_record_semantics(record)
    except Exception:
        raise ValueError("repository record semantics are invalid") from None
    return dict(record)


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    payload_text = str(row["payload"])
    envelope = _strict_json_object(
        payload_text.encode("utf-8"), name="stored artifact payload"
    )
    record = {
        "module": str(row["module"]),
        "object_type": str(row["object_type"]),
        "object_id": str(row["object_id"]),
        "object_version": str(row["object_version"]),
        "status": str(row["status"]),
        "content_checksum": str(row["content_checksum"]),
        "payload_version": str(row["payload_version"]),
        "payload_checksum": str(row["payload_checksum"]),
        "payload": envelope,
    }
    return _validate_record(record)


class SQLiteM1M2M3Repository:
    """Single authoritative SQLite adapter for complete M1/M2/M3 artifacts."""

    backend_name = "sqlite"

    def __init__(self, database_path: Path | str) -> None:
        self._database_path = Path(database_path)

    @property
    def database_path(self) -> Path:
        """Return the configured database path for local adapter use."""

        return self._database_path

    def initialize(self) -> None:
        """Create the base database and apply the S1-S6 schema transactionally."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def schema_is_current(self) -> bool:
        """Return whether the independent S1-S6 schema is complete."""

        try:
            connection = connect_sqlite(self._database_path)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return False
        try:
            if current_s1_s6_schema_version(connection) != S1_S6_SCHEMA_VERSION:
                return False
            try:
                validate_schema(connection)
            except RuntimeError:
                return False
            return True
        except (OSError, sqlite3.Error):
            return False
        finally:
            connection.close()

    def is_ready(self) -> bool:
        """Return readiness without exposing paths or database exceptions."""

        return self.schema_is_current()

    def readiness(self) -> BackendReadiness:
        """Return a safe readiness value for the application port."""

        ready = self.is_ready()
        return BackendReadiness(
            backend=self.backend_name,
            ready=ready,
            reason=None if ready else "schema_unavailable",
        )

    def save_course_package(self, package: CoursePackage) -> None:
        """Reject incomplete package-only writes."""

        del package
        raise _invalid(
            "m1",
            "COURSE_ARTIFACT_INVALID",
            "course import persistence requires the complete input snapshot",
        )

    def save_course_import(
        self, package: CoursePackage, snapshot: CourseImportSnapshot
    ) -> None:
        try:
            codec = _m1_codec()
            payloads, metadata = codec._serialize(package, snapshot)
            restored = codec._deserialize(
                payloads,
                metadata,
                package.course_package_id,
                package.package_version,
            )
            if restored != package:
                raise ValueError("M1 serializer did not round-trip the package")
            envelope = _encode_payload_envelope(payloads, metadata)
            self._save_record(
                _record(
                    module="m1",
                    object_type="course_import",
                    object_id=package.course_package_id,
                    object_version=package.package_version,
                    status=package.status,
                    content_checksum=package.checksum,
                    envelope=envelope,
                )
            )
        except DomainError:
            raise
        except Exception:
            raise _invalid(
                "m1",
                "COURSE_ARTIFACT_INVALID",
                "course package artifact is invalid",
            ) from None

    def load_course_import(
        self, course_package_id: str, package_version: str
    ) -> tuple[CoursePackage, CourseImportSnapshot] | None:
        record = self._load_record(
            "m1", "course_import", course_package_id, package_version
        )
        if record is None:
            return None
        try:
            payloads, metadata = _decode_payload_envelope(
                record["payload"],
                canonical_json=dumps_json(record["payload"]).encode("utf-8"),
            )
            codec = _m1_codec()
            package = codec._deserialize(
                payloads, metadata, course_package_id, package_version
            )
            if (
                package.status != record["status"]
                or package.checksum != record["content_checksum"]
            ):
                raise ValueError("M1 record bindings differ")
            return package, _m1_snapshot(package, payloads)
        except Exception:
            raise _invalid(
                "m1",
                "COURSE_ARTIFACT_INVALID",
                "course package artifact is invalid",
            ) from None

    def get_course_package(
        self, course_package_id: str, package_version: str
    ) -> CoursePackage | None:
        loaded = self.load_course_import(course_package_id, package_version)
        return None if loaded is None else loaded[0]

    def save_index(self, index: EvidenceIndexRef) -> None:
        """Reject metadata-only writes which cannot restore the index."""

        del index
        raise _invalid(
            "m2",
            "INDEX_ARTIFACT_INVALID",
            "evidence index persistence requires the complete index artifact",
        )

    def save_index_artifact(
        self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> None:
        try:
            codec = _m2_codec()
            payloads, metadata = codec._serialize(index, snapshot)
            restored = codec._deserialize(
                payloads, metadata, index.index_id, index.index_version
            )
            if restored != (index, snapshot):
                raise ValueError("M2 serializer did not round-trip the artifact")
            self._save_record(
                _record(
                    module="m2",
                    object_type="evidence_index",
                    object_id=index.index_id,
                    object_version=index.index_version,
                    status=index.status,
                    content_checksum=index.checksum,
                    envelope=_encode_payload_envelope(payloads, metadata),
                )
            )
        except DomainError:
            raise
        except Exception:
            raise _invalid(
                "m2",
                "INDEX_ARTIFACT_INVALID",
                "evidence index artifact is invalid",
            ) from None

    def load_index_artifact(
        self, index_id: str, index_version: str
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        record = self._load_record("m2", "evidence_index", index_id, index_version)
        if record is None:
            return None
        try:
            payloads, metadata = _decode_payload_envelope(record["payload"])
            codec = _m2_codec()
            index, snapshot = codec._deserialize(
                payloads, metadata, index_id, index_version
            )
            if (
                index.status != record["status"]
                or index.checksum != record["content_checksum"]
            ):
                raise ValueError("M2 record bindings differ")
            return index, snapshot
        except Exception:
            raise _invalid(
                "m2",
                "INDEX_ARTIFACT_INVALID",
                "evidence index artifact is invalid",
            ) from None

    def get_index(
        self, index_id: str, index_version: str
    ) -> EvidenceIndexRef | None:
        loaded = self.load_index_artifact(index_id, index_version)
        return None if loaded is None else loaded[0]

    def save_retrieval_audit(self, envelope: RetrievalAuditEnvelope) -> None:
        """Persist one redacted retrieval audit as an immutable record."""

        try:
            payload = envelope.to_payload_text().encode("utf-8")
            self._save_record(
                _record(
                    module="m2",
                    object_type="retrieval_audit",
                    object_id=envelope.audit.audit_id,
                    object_version="v1",
                    status=envelope.audit.status,
                    content_checksum=_sha256(payload),
                    envelope=_encode_payload_envelope({"audit.json": payload}, {}),
                )
            )
        except DomainError:
            raise
        except Exception:
            raise _persistence_invalid() from None

    def load_retrieval_audit(
        self, audit_id: str, audit_version: str = "v1"
    ) -> RetrievalAuditEnvelope | None:
        if audit_version != "v1":
            raise _persistence_invalid()
        record = self._load_record("m2", "retrieval_audit", audit_id, audit_version)
        if record is None:
            return None
        try:
            payloads, _metadata = _decode_payload_envelope(record["payload"])
            envelope = RetrievalAuditEnvelope.from_payload_text(payloads["audit.json"])
            if (
                envelope.audit.audit_id != audit_id
                or envelope.audit.status != record["status"]
                or _sha256(envelope.to_payload_text().encode("utf-8"))
                != record["content_checksum"]
            ):
                raise ValueError
            return envelope
        except Exception:
            raise _persistence_invalid() from None

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        """Reject bundle-only writes which bypass validation evidence."""

        del bundle
        raise _invalid(
            "m3",
            "KNOWLEDGE_ARTIFACT_INVALID",
            "knowledge persistence requires the complete publication artifact",
        )

    def get_knowledge_bundle(
        self, knowledge_bundle_id: str, bundle_version: str
    ) -> KnowledgeBundle | None:
        loaded = self.load_bundle_artifact(knowledge_bundle_id, bundle_version)
        return None if loaded is None else loaded[0]

    def save_bundle_artifact(
        self,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            payloads, metadata = FileM3Repository._approved_payloads(
                bundle, report, snapshot
            )
            restored = FileM3Repository._decode_approved(
                payloads,
                metadata,
                bundle.knowledge_bundle_id,
                bundle.bundle_version,
            )
            if restored != (bundle, report, snapshot):
                raise ValueError("M3 serializer did not round-trip the artifact")
            self._save_record(
                _record(
                    module="m3",
                    object_type="knowledge_bundle",
                    object_id=bundle.knowledge_bundle_id,
                    object_version=bundle.bundle_version,
                    status=bundle.status,
                    content_checksum=bundle.content_checksum(),
                    envelope=_encode_payload_envelope(payloads, metadata),
                )
            )
        except DomainError:
            raise
        except Exception:
            raise _invalid(
                "m3",
                "KNOWLEDGE_ARTIFACT_INVALID",
                "knowledge artifact is invalid",
            ) from None

    def load_bundle_artifact(
        self, knowledge_bundle_id: str, bundle_version: str
    ) -> tuple[KnowledgeBundle, M3ValidationReport, M3SeedSnapshot] | None:
        record = self._load_record(
            "m3", "knowledge_bundle", knowledge_bundle_id, bundle_version
        )
        if record is None:
            return None
        try:
            payloads, metadata = _decode_payload_envelope(record["payload"])
            bundle, report, snapshot = FileM3Repository._decode_approved(
                payloads,
                metadata,
                knowledge_bundle_id,
                bundle_version,
            )
            if (
                bundle.status != record["status"]
                or bundle.content_checksum() != record["content_checksum"]
            ):
                raise ValueError("M3 record bindings differ")
            return bundle, report, snapshot
        except Exception:
            raise _invalid(
                "m3",
                "KNOWLEDGE_ARTIFACT_INVALID",
                "knowledge artifact is invalid",
            ) from None

    def save_rejected_validation(
        self, report: M3ValidationReport, snapshot: M3SeedSnapshot
    ) -> None:
        try:
            payloads, metadata = FileM3Repository._rejected_payloads(
                report, snapshot
            )
            restored = FileM3Repository._decode_rejected(
                payloads,
                metadata,
                report.course_package_id,
                report.report_id,
            )
            if restored != (report, snapshot):
                raise ValueError("M3 serializer did not round-trip rejection")
            self._save_record(
                _record(
                    module="m3",
                    object_type="knowledge_validation",
                    object_id=report.course_package_id,
                    object_version=report.report_id,
                    status=report.status,
                    content_checksum=report.checksum,
                    envelope=_encode_payload_envelope(payloads, metadata),
                )
            )
        except DomainError:
            raise
        except Exception:
            raise _invalid(
                "m3",
                "KNOWLEDGE_ARTIFACT_INVALID",
                "knowledge validation artifact is invalid",
            ) from None

    def load_rejected_validation(
        self, course_package_id: str, report_id: str
    ) -> tuple[M3ValidationReport, M3SeedSnapshot] | None:
        record = self._load_record(
            "m3", "knowledge_validation", course_package_id, report_id
        )
        if record is None:
            return None
        try:
            payloads, metadata = _decode_payload_envelope(record["payload"])
            report, snapshot = FileM3Repository._decode_rejected(
                payloads, metadata, course_package_id, report_id
            )
            if (
                report.status != record["status"]
                or report.checksum != record["content_checksum"]
            ):
                raise ValueError("M3 rejection bindings differ")
            return report, snapshot
        except Exception:
            raise _invalid(
                "m3",
                "KNOWLEDGE_ARTIFACT_INVALID",
                "knowledge validation artifact is invalid",
            ) from None

    def save_teacher_review(self, review: TeacherReviewRecord) -> None:
        """Persist one immutable workflow version."""

        try:
            payload = review.to_payload_text().encode("utf-8")
            self._save_record(
                _record(
                    module="m3",
                    object_type="teacher_review",
                    object_id=review.review_id,
                    object_version=str(review.version),
                    status=review.state,
                    content_checksum=_sha256(payload),
                    envelope=_encode_payload_envelope({"review.json": payload}, {}),
                )
            )
        except DomainError:
            raise
        except Exception:
            raise _persistence_invalid() from None

    def get_teacher_review(self, review_id: str) -> TeacherReviewRecord | None:
        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                f"""
                SELECT module, object_type, object_id, object_version,
                       status, content_checksum, payload_version,
                       payload_checksum, payload
                FROM {_TABLE}
                WHERE module = 'm3' AND object_type = 'teacher_review'
                  AND object_id = ?
                ORDER BY CAST(object_version AS INTEGER) DESC
                """,
                (review_id,),
            ).fetchall()
        except sqlite3.Error:
            raise _persistence_invalid() from None
        finally:
            connection.close()
        if not rows:
            return None
        try:
            record = _row_to_record(rows[0])
            payloads, _metadata = _decode_payload_envelope(record["payload"])
            return TeacherReviewRecord.from_payload_text(payloads["review.json"])
        except Exception:
            raise _persistence_invalid() from None

    def compare_and_swap_teacher_review(
        self,
        review_id: str,
        expected_version: int,
        review: TeacherReviewRecord,
    ) -> bool:
        """Atomically append a review version only when the latest version matches."""

        if review.review_id != review_id or review.version != expected_version + 1:
            raise _persistence_invalid()
        prepared = _validate_record(
            _record(
                module="m3",
                object_type="teacher_review",
                object_id=review.review_id,
                object_version=str(review.version),
                status=review.state,
                content_checksum=_sha256(review.to_payload_text().encode("utf-8")),
                envelope=_encode_payload_envelope(
                    {"review.json": review.to_payload_text().encode("utf-8")}, {}
                ),
            )
        )
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT module, object_type, object_id, object_version,
                       status, content_checksum, payload_version,
                       payload_checksum, payload
                FROM {_TABLE}
                WHERE module = 'm3' AND object_type = 'teacher_review'
                  AND object_id = ?
                ORDER BY CAST(object_version AS INTEGER) DESC
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return False
            current = _row_to_record(row)
            if int(current["object_version"]) != expected_version:
                connection.execute("ROLLBACK")
                return False
            self._insert_or_replay(connection, prepared)
            connection.execute("COMMIT")
            return True
        except DomainError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise _persistence_invalid() from None
        finally:
            connection.close()

    def export_manifest(self) -> dict[str, Any]:
        """Export every complete record as a checksummed portable manifest."""

        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                f"""
                SELECT module, object_type, object_id, object_version,
                       status, content_checksum, payload_version,
                       payload_checksum, payload
                FROM {_TABLE}
                ORDER BY module, object_type, object_id, object_version
                """
            ).fetchall()
            records = [_row_to_record(row) for row in rows]
        except sqlite3.Error:
            raise _persistence_invalid() from None
        finally:
            connection.close()
        core = {
            "format": _MANIFEST_FORMAT,
            "schema_version": S1_S6_SCHEMA_VERSION,
            "records": records,
        }
        return {
            **core,
            "manifest_checksum": _sha256(dumps_json(core).encode("utf-8")),
        }

    def import_manifest(self, manifest: Mapping[str, Any] | str | bytes) -> int:
        """Validate the entire manifest, then publish it in one transaction."""

        try:
            value = self._manifest_value(manifest)
            if set(value) != {
                "format",
                "schema_version",
                "records",
                "manifest_checksum",
            }:
                raise ValueError("manifest keys are invalid")
            if value["format"] != _MANIFEST_FORMAT:
                raise ValueError("manifest format is invalid")
            if value["schema_version"] != S1_S6_SCHEMA_VERSION:
                raise ValueError("manifest schema version is invalid")
            records_value = value["records"]
            if type(records_value) is not list:
                raise ValueError("manifest records are invalid")
            core = {
                "format": value["format"],
                "schema_version": value["schema_version"],
                "records": records_value,
            }
            if value["manifest_checksum"] != _sha256(
                dumps_json(core).encode("utf-8")
            ):
                raise ValueError("manifest checksum is invalid")
            records = [_validate_record(record) for record in records_value]
            identities: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for record in records:
                identity = self._identity(record)
                previous = identities.get(identity)
                if previous is not None and previous != record:
                    raise _conflict()
                identities[identity] = record
            self._save_records(tuple(identities.values()))
            return len(identities)
        except DomainError:
            raise
        except Exception:
            raise _persistence_invalid() from None

    # Compatibility aliases keep the migration boundary discoverable without
    # introducing a second persistence implementation.
    export_artifact_manifest = export_manifest
    import_artifact_manifest = import_manifest

    def _manifest_value(
        self, manifest: Mapping[str, Any] | str | bytes
    ) -> dict[str, Any]:
        if isinstance(manifest, Mapping):
            if type(manifest) is not dict:
                return dict(manifest)
            return dict(manifest)
        if isinstance(manifest, str):
            manifest = manifest.encode("utf-8")
        if type(manifest) is not bytes:
            raise ValueError("manifest must be a mapping or JSON document")
        return _strict_json_object(manifest, name="repository manifest")

    @staticmethod
    def _identity(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(record["module"]),
            str(record["object_type"]),
            str(record["object_id"]),
            str(record["object_version"]),
        )

    def _load_record(
        self,
        module: str,
        object_type: str,
        object_id: str,
        object_version: str,
    ) -> dict[str, Any] | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                f"""
                SELECT module, object_type, object_id, object_version,
                       status, content_checksum, payload_version,
                       payload_checksum, payload
                FROM {_TABLE}
                WHERE module = ? AND object_type = ?
                  AND object_id = ? AND object_version = ?
                """,
                (module, object_type, object_id, object_version),
            ).fetchone()
        except sqlite3.Error:
            raise _persistence_invalid() from None
        finally:
            connection.close()
        if row is None:
            return None
        try:
            return _row_to_record(row)
        except Exception:
            raise _persistence_invalid() from None

    def _save_record(self, record: dict[str, Any]) -> None:
        self._save_records((record,))

    def _save_records(self, records: tuple[dict[str, Any], ...]) -> None:
        prepared = tuple(_validate_record(record) for record in records)
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in prepared:
                self._insert_or_replay(connection, record)
            connection.execute("COMMIT")
        except DomainError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise _persistence_invalid() from None
        finally:
            connection.close()

    def _insert_or_replay(
        self, connection: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        identity = self._identity(record)
        row = connection.execute(
            f"""
            SELECT module, object_type, object_id, object_version,
                   status, content_checksum, payload_version,
                   payload_checksum, payload
            FROM {_TABLE}
            WHERE module = ? AND object_type = ?
              AND object_id = ? AND object_version = ?
            """,
            identity,
        ).fetchone()
        if row is not None:
            try:
                existing = _row_to_record(row)
            except Exception:
                raise _persistence_invalid() from None
            if existing == dict(record):
                return
            raise _conflict()
        envelope_json = dumps_json(record["payload"])
        connection.execute(
            f"""
            INSERT INTO {_TABLE}(
                module, object_type, object_id, object_version,
                status, content_checksum, payload_version,
                payload_checksum, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["module"],
                record["object_type"],
                record["object_id"],
                record["object_version"],
                record["status"],
                record["content_checksum"],
                record["payload_version"],
                record["payload_checksum"],
                envelope_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


SQLiteS1S6Repository = SQLiteM1M2M3Repository


__all__ = ["SQLiteM1M2M3Repository", "SQLiteS1S6Repository"]
