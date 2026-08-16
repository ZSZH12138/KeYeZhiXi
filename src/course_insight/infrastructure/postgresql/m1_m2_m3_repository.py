"""Production PostgreSQL adapters for the M1-M3 S1-S6 boundary.

This module owns one authoritative PostgreSQL persistence boundary for the
complete M1 import, M2 index and M3 publication artifacts.  The existing file
repositories remain the canonical codecs; this adapter only changes the
durable store.  Every write validates the complete payload before entering a
transaction, and every read validates the payload and its checksums again.

The pgvector adapter lives beside the artifact adapter so a deployment can
bind both ports to the same pool without introducing a second persistence
authority.  It intentionally uses an unconstrained ``vector`` column plus an
explicit per-index dimension: embedding providers may be migrated without a
DDL rewrite, while every write and query still enforces exact dimensions.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from contextlib import contextmanager
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any, Callable, ClassVar, TypeVar
import weakref

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.m1_file_repository import FileM1Repository
from course_insight.infrastructure.m2_file_repository import FileM2Repository
from course_insight.infrastructure.m3_file_repository import FileM3Repository
from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    run_migrations,
    schema_is_current,
)
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    MAX_PGVECTOR_DIMENSION,
    MAX_VECTOR_BATCH_SIZE,
    VectorDocument,
    VectorIndexMetadata,
    VectorMatch,
    build_pgvector_exact_search_sql,
)
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationReport,
)


_ARTIFACT_TABLE = "m1_m2_m3_artifacts"
_PAYLOAD_VERSION = "s1_s6/v1"
_PAYLOAD_FORMAT = "s1_s6_payload/v1"
_MANIFEST_FORMAT = "s1_s6_repository_manifest/v1"
# The manifest format is shared by SQLite and PostgreSQL.  It is deliberately
# independent from either backend's migration ledger so export/import remains
# a real migration boundary rather than a same-database backup shortcut.
_MANIFEST_SCHEMA_VERSION = 1
_SHA256_ALPHABET = frozenset("0123456789abcdef")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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
_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL artifact integrity failure"
_CONFLICT_ERROR = "PostgreSQL artifact identity conflict"
_VECTOR_OPERATION_ERROR = "PostgreSQL vector operation failed"
_VECTOR_INTEGRITY_ERROR = "PostgreSQL vector integrity failure"
_T = TypeVar("_T")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _invalid(module: str, code: str, message: str) -> DomainError:
    return DomainError(code=code, module=module, message=message)


def _artifact_invalid(module: str) -> DomainError:
    codes = {
        "m1": ("COURSE_ARTIFACT_INVALID", "course package artifact is invalid"),
        "m2": ("INDEX_ARTIFACT_INVALID", "evidence index artifact is invalid"),
        "m3": ("KNOWLEDGE_ARTIFACT_INVALID", "knowledge artifact is invalid"),
    }
    code, message = codes[module]
    return _invalid(module, code, message)


def _run_database(operation: Callable[[], _T]) -> _T:
    """Run one adapter operation with stable, secret-free error mapping."""

    try:
        return operation()
    except PostgresError:
        raise
    except DomainError:
        raise
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise PostgresConnectionError(
            "PostgreSQL connection is unavailable"
        ) from None
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None
    except Exception:
        # A pool implementation may surface a non-psycopg transport exception.
        # Do not allow driver details, DSNs or payload text to escape this port.
        raise PostgresOperationError(_OPERATION_ERROR) from None


def _run_vector(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except PostgresError:
        raise
    except (psycopg.OperationalError, psycopg.InterfaceError):
        raise PostgresConnectionError(
            "PostgreSQL connection is unavailable"
        ) from None
    except psycopg.Error:
        raise PostgresOperationError(_VECTOR_OPERATION_ERROR) from None
    except Exception:
        raise PostgresOperationError(_VECTOR_OPERATION_ERROR) from None


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and _SHA256_RE.fullmatch(value) is not None
        and set(value) <= _SHA256_ALPHABET
    )


def _safe_identity(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and "\x00" not in value
    )


def _safe_payload_path(value: object) -> bool:
    if not _safe_identity(value):
        return False
    assert isinstance(value, str)
    if value.startswith(("/", "\\")) or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _json_value(value: object) -> object:
    """Unwrap psycopg's Jsonb wrapper while staying fake-row friendly."""

    return getattr(value, "obj", value)


def _strict_json_object(value: object, *, name: str) -> dict[str, Any]:
    value = _json_value(value)
    raw_bytes: bytes | None = None
    if type(value) is bytes:
        try:
            raw_bytes = value
            value = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ValueError(f"{name} is not valid JSON") from None
    elif type(value) is str:
        try:
            raw_bytes = value.encode("utf-8")
            value = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (json.JSONDecodeError, ValueError):
            raise ValueError(f"{name} is not valid JSON") from None
    if type(value) is not dict:
        raise ValueError(f"{name} must be an object")
    # JSONB has already parsed the tree.  Re-serializing still rejects NaN,
    # unsupported values and recursive structures before validation continues.
    dumps_json(value)
    if raw_bytes is not None and dumps_json(value).encode("utf-8") != raw_bytes:
        raise ValueError(f"{name} is not canonical JSON")
    return dict(value)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("JSON object contains duplicate keys")
        result[key] = item
    return result


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
    dumps_json(envelope)
    return envelope


def _decode_payload_envelope(
    envelope: object,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    envelope = _strict_json_object(envelope, name="stored artifact payload")
    if set(envelope) != _PAYLOAD_KEYS or envelope["format"] != _PAYLOAD_FORMAT:
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
    return payloads, dict(envelope["metadata"])


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


def _m1_codec() -> FileM1Repository:
    """Construct the pure FileM1 codec without creating a file backend."""

    return object.__new__(FileM1Repository)


def _m2_codec() -> FileM2Repository:
    """Construct the pure FileM2 codec without creating a file backend."""

    return object.__new__(FileM2Repository)


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
    if any(
        not _safe_identity(value)
        for value in (module, object_type, object_id, object_version, status)
    ):
        raise ValueError("repository record identity is invalid")
    if not _is_sha256(content_checksum):
        raise ValueError("repository content checksum is invalid")
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
    payloads, metadata = _decode_payload_envelope(record["payload"])
    module = record["module"]
    object_type = record["object_type"]
    if module == "m1" and object_type == "course_import":
        package = _m1_codec()._deserialize(
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
        index, _snapshot = _m2_codec()._deserialize(
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
        if not _safe_identity(record[key]):
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
    if not _is_sha256(record["payload_checksum"]):
        raise ValueError("repository payload checksum is invalid")
    _validate_record_semantics(record)
    return dict(record)


def _row_to_record(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        record = {
            "module": row["module"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "object_version": row["object_version"],
            "status": row["status"],
            "content_checksum": row["content_checksum"],
            "payload_version": row["payload_version"],
            "payload_checksum": row["payload_checksum"],
            "payload": _strict_json_object(
                row["payload"], name="stored artifact payload"
            ),
        }
    except (KeyError, TypeError):
        raise ValueError("stored artifact row is incomplete") from None
    return _validate_record(record)


_ARTIFACT_COLUMNS = """
module, object_type, object_id, object_version,
status, content_checksum, payload_version, payload_checksum, payload
"""


class PostgresM1M2M3Repository:
    """One authoritative PostgreSQL adapter for complete M1/M2/M3 artifacts."""

    backend_name = "postgresql"
    _instances: ClassVar[
        weakref.WeakValueDictionary[int, "PostgresM1M2M3Repository"]
    ] = weakref.WeakValueDictionary()

    def __new__(cls, pool: object) -> "PostgresM1M2M3Repository":
        """Reuse one adapter identity for one live pool.

        The composition root wires M1, M2 and M3 independently.  Pool-keyed
        reuse keeps that wiring a single authority without changing the
        application layer, while weak values prevent a closed test pool from
        becoming a process-lifetime registry entry.
        """

        key = id(pool)
        cached = cls._instances.get(key)
        if cached is not None and getattr(cached, "_pool", None) is pool:
            return cached
        instance = super().__new__(cls)
        try:
            cls._instances[key] = instance
        except TypeError:
            # A non-weakrefable custom test pool still receives a correct
            # adapter; it simply cannot participate in identity reuse.
            pass
        return instance

    def __init__(self, pool: object) -> None:
        self._pool = pool
        if not hasattr(self, "_active_connection"):
            self._active_connection: ContextVar[Any | None] = ContextVar(
                f"postgres_m1_m2_m3_connection_{id(self)}",
                default=None,
            )

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        """Reuse the current transaction connection within this repository."""

        active_connection = self._active_connection.get()
        if active_connection is not None:
            yield active_connection
            return

        with self._pool.connection() as connection:  # type: ignore[attr-defined]
            token = self._active_connection.set(connection)
            try:
                yield connection
            finally:
                self._active_connection.reset(token)

    def initialize(self) -> None:
        """Apply the checksum-locked PostgreSQL migration chain."""

        run_migrations(self._pool)  # type: ignore[arg-type]

    def schema_is_current(self) -> bool:
        """Return the verified migration state without a second backend."""

        return schema_is_current(self._pool)  # type: ignore[arg-type]

    def is_ready(self) -> bool:
        return self.schema_is_current()

    def readiness(self) -> object:
        # Import lazily: application.factory supplies this adapter, while the
        # application package also exposes persistence ports from __init__.
        # A top-level import here would create a package-initialization cycle.
        from course_insight.application.persistence import BackendReadiness

        ready = self.is_ready()
        return BackendReadiness(
            backend=self.backend_name,
            ready=ready,
            reason=None if ready else "schema_unavailable",
        )

    @property
    def vector_store(self) -> "PostgresPgVectorStore":
        """Return the pgvector port bound to this repository's pool."""

        return PostgresPgVectorStore(self._pool)

    def save_course_package(self, package: CoursePackage) -> None:
        del package
        raise _artifact_invalid("m1")

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
            record = _record(
                module="m1",
                object_type="course_import",
                object_id=package.course_package_id,
                object_version=package.package_version,
                status=package.status,
                content_checksum=package.checksum,
                envelope=_encode_payload_envelope(payloads, metadata),
            )
        except DomainError:
            raise
        except Exception:
            raise _artifact_invalid("m1") from None
        self._save_record(record)

    def load_course_import(
        self, course_package_id: str, package_version: str
    ) -> tuple[CoursePackage, CourseImportSnapshot] | None:
        record = self._load_record(
            "m1", "course_import", course_package_id, package_version
        )
        if record is None:
            return None
        try:
            payloads, metadata = _decode_payload_envelope(record["payload"])
            package = _m1_codec()._deserialize(
                payloads, metadata, course_package_id, package_version
            )
            if (
                package.status != record["status"]
                or package.checksum != record["content_checksum"]
            ):
                raise ValueError("M1 record bindings differ")
            return package, _m1_snapshot(package, payloads)
        except Exception:
            raise PostgresOperationError(_INTEGRITY_ERROR) from None

    def get_course_package(
        self, course_package_id: str, package_version: str
    ) -> CoursePackage | None:
        loaded = self.load_course_import(course_package_id, package_version)
        return None if loaded is None else loaded[0]

    def save_index(self, index: EvidenceIndexRef) -> None:
        del index
        raise _artifact_invalid("m2")

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
            record = _record(
                module="m2",
                object_type="evidence_index",
                object_id=index.index_id,
                object_version=index.index_version,
                status=index.status,
                content_checksum=index.checksum,
                envelope=_encode_payload_envelope(payloads, metadata),
            )
        except DomainError:
            raise
        except Exception:
            raise _artifact_invalid("m2") from None
        self._save_record(record)

    def load_index_artifact(
        self, index_id: str, index_version: str
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        record = self._load_record("m2", "evidence_index", index_id, index_version)
        if record is None:
            return None
        try:
            payloads, metadata = _decode_payload_envelope(record["payload"])
            index, snapshot = _m2_codec()._deserialize(
                payloads, metadata, index_id, index_version
            )
            if (
                index.status != record["status"]
                or index.checksum != record["content_checksum"]
            ):
                raise ValueError("M2 record bindings differ")
            return index, snapshot
        except Exception:
            raise PostgresOperationError(_INTEGRITY_ERROR) from None

    def get_index(
        self, index_id: str, index_version: str
    ) -> EvidenceIndexRef | None:
        loaded = self.load_index_artifact(index_id, index_version)
        return None if loaded is None else loaded[0]

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        del bundle
        raise _artifact_invalid("m3")

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
            record = _record(
                module="m3",
                object_type="knowledge_bundle",
                object_id=bundle.knowledge_bundle_id,
                object_version=bundle.bundle_version,
                status=bundle.status,
                content_checksum=bundle.content_checksum(),
                envelope=_encode_payload_envelope(payloads, metadata),
            )
        except DomainError:
            raise
        except Exception:
            raise _artifact_invalid("m3") from None
        self._save_record(record)

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
            raise PostgresOperationError(_INTEGRITY_ERROR) from None

    def save_rejected_validation(
        self, report: M3ValidationReport, snapshot: M3SeedSnapshot
    ) -> None:
        try:
            payloads, metadata = FileM3Repository._rejected_payloads(
                report, snapshot
            )
            restored = FileM3Repository._decode_rejected(
                payloads, metadata, report.course_package_id, report.report_id
            )
            if restored != (report, snapshot):
                raise ValueError("M3 serializer did not round-trip rejection")
            record = _record(
                module="m3",
                object_type="knowledge_validation",
                object_id=report.course_package_id,
                object_version=report.report_id,
                status=report.status,
                content_checksum=report.checksum,
                envelope=_encode_payload_envelope(payloads, metadata),
            )
        except DomainError:
            raise
        except Exception:
            raise _artifact_invalid("m3") from None
        self._save_record(record)

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
            raise PostgresOperationError(_INTEGRITY_ERROR) from None

    def export_manifest(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with self._connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_ARTIFACT_COLUMNS}
                    FROM {_ARTIFACT_TABLE}
                    ORDER BY module, object_type, object_id, object_version
                    """
                ).fetchall()
            records = [_row_to_record(row) for row in rows]
            core = {
                "format": _MANIFEST_FORMAT,
                "schema_version": _MANIFEST_SCHEMA_VERSION,
                "records": records,
            }
            return {
                **core,
                "manifest_checksum": _sha256(
                    dumps_json(core).encode("utf-8")
                ),
            }

        return _run_database(operation)

    def import_manifest(self, manifest: Mapping[str, Any] | str | bytes) -> int:
        def operation() -> int:
            value = _manifest_value(manifest)
            if set(value) != {
                "format",
                "schema_version",
                "records",
                "manifest_checksum",
            }:
                raise ValueError("manifest keys are invalid")
            if value["format"] != _MANIFEST_FORMAT:
                raise ValueError("manifest format is invalid")
            if value["schema_version"] != _MANIFEST_SCHEMA_VERSION:
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
                identity = _identity(record)
                previous = identities.get(identity)
                if previous is not None and previous != record:
                    raise PostgresOperationError(_CONFLICT_ERROR)
                identities[identity] = record
            self._save_records(tuple(identities.values()))
            return len(identities)

        return _run_database(operation)

    # Compatibility aliases keep the migration boundary discoverable.
    export_artifact_manifest = export_manifest
    import_artifact_manifest = import_manifest

    def _load_record(
        self,
        module: str,
        object_type: str,
        object_id: str,
        object_version: str,
    ) -> dict[str, Any] | None:
        def operation() -> dict[str, Any] | None:
            with self._connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_ARTIFACT_COLUMNS}
                    FROM {_ARTIFACT_TABLE}
                    WHERE module = %s AND object_type = %s
                      AND object_id = %s AND object_version = %s
                    """,
                    (module, object_type, object_id, object_version),
                ).fetchone()
            if row is None:
                return None
            try:
                return _row_to_record(row)
            except Exception:
                raise PostgresOperationError(_INTEGRITY_ERROR) from None

        return _run_database(operation)

    def _save_record(self, record: dict[str, Any]) -> None:
        self._save_records((record,))

    def _save_records(self, records: tuple[dict[str, Any], ...]) -> None:
        try:
            prepared = tuple(_validate_record(record) for record in records)
        except PostgresError:
            raise
        except Exception:
            raise PostgresOperationError(_INTEGRITY_ERROR) from None

        def operation() -> None:
            with self._connection() as connection:
                with connection.transaction():
                    for record in prepared:
                        self._insert_or_replay(connection, record)

        _run_database(operation)

    @staticmethod
    def _insert_or_replay(connection: Any, record: Mapping[str, Any]) -> None:
        identity = _identity(record)
        row = connection.execute(
            f"""
            SELECT {_ARTIFACT_COLUMNS}
            FROM {_ARTIFACT_TABLE}
            WHERE module = %s AND object_type = %s
              AND object_id = %s AND object_version = %s
            FOR UPDATE
            """,
            identity,
        ).fetchone()
        if row is not None:
            try:
                existing = _row_to_record(row)
            except Exception:
                raise PostgresOperationError(_INTEGRITY_ERROR) from None
            if existing == dict(record):
                return
            raise PostgresOperationError(_CONFLICT_ERROR)

        connection.execute(
            f"""
            INSERT INTO {_ARTIFACT_TABLE}(
                module, object_type, object_id, object_version,
                status, content_checksum, payload_version,
                payload_checksum, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
                module, object_type, object_id, object_version
            ) DO NOTHING
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
                Jsonb(record["payload"]),
            ),
        )
        authoritative = connection.execute(
            f"""
            SELECT {_ARTIFACT_COLUMNS}
            FROM {_ARTIFACT_TABLE}
            WHERE module = %s AND object_type = %s
              AND object_id = %s AND object_version = %s
            FOR UPDATE
            """,
            identity,
        ).fetchone()
        if authoritative is None:
            raise PostgresOperationError(_INTEGRITY_ERROR)
        try:
            existing = _row_to_record(authoritative)
        except Exception:
            raise PostgresOperationError(_INTEGRITY_ERROR) from None
        if existing != dict(record):
            raise PostgresOperationError(_CONFLICT_ERROR)

    # -- retrieval audit persistence -------------------------------------

    def save_retrieval_audit(self, envelope: object) -> None:
        """Persist the redacted retrieval audit envelope idempotently."""

        from course_insight.modules.m2_evidence_retrieval.audit import (
            RetrievalAuditEnvelope,
        )

        if not isinstance(envelope, RetrievalAuditEnvelope):
            raise DomainError(
                code="RETRIEVAL_AUDIT_PERSISTENCE_FAILED",
                module="m2",
                message="retrieval audit envelope is invalid",
            )
        try:
            payload = json.loads(envelope.to_payload_text())
            payload_bytes = dumps_json(payload).encode("utf-8")
            _validate_retrieval_audit_payload(payload)
        except Exception:
            raise DomainError(
                code="RETRIEVAL_AUDIT_PERSISTENCE_FAILED",
                module="m2",
                message="retrieval audit envelope is invalid",
            ) from None

        def operation() -> None:
            with self._connection() as connection:
                with connection.transaction():
                    existing = connection.execute(
                        """
                        SELECT payload, payload_checksum
                        FROM m2_retrieval_audits
                        WHERE audit_id = %s
                        FOR UPDATE
                        """,
                        (envelope.audit.audit_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            _json_value(existing["payload"]) != payload
                            or existing["payload_checksum"] != _sha256(payload_bytes)
                        ):
                            raise PostgresOperationError(_CONFLICT_ERROR)
                        return
                    connection.execute(
                        """
                        INSERT INTO m2_retrieval_audits(
                            audit_id, query_id, index_id, index_version,
                            policy_id, status, created_at,
                            payload, payload_checksum
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (audit_id) DO NOTHING
                        """,
                        (
                            envelope.audit.audit_id,
                            envelope.audit.query_id,
                            envelope.audit.index_id,
                            envelope.metadata.index_version,
                            envelope.audit.policy_id,
                            envelope.audit.status,
                            envelope.audit.created_at,
                            Jsonb(payload),
                            _sha256(payload_bytes),
                        ),
                    )

        _run_database(operation)

    def load_retrieval_audit(self, audit_id: str) -> object | None:
        from course_insight.modules.m2_evidence_retrieval.audit import (
            RetrievalAuditEnvelope,
        )

        def operation() -> object | None:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT payload, payload_checksum
                    FROM m2_retrieval_audits
                    WHERE audit_id = %s
                    """,
                    (audit_id,),
                ).fetchone()
            if row is None:
                return None
            payload = _strict_json_object(row["payload"], name="audit payload")
            payload_bytes = dumps_json(payload).encode("utf-8")
            if row["payload_checksum"] != _sha256(payload_bytes):
                raise PostgresOperationError(_INTEGRITY_ERROR)
            try:
                envelope = RetrievalAuditEnvelope.from_payload_text(payload_bytes)
                if envelope.audit.audit_id != audit_id:
                    raise ValueError("audit identity differs")
                return envelope
            except Exception:
                raise PostgresOperationError(_INTEGRITY_ERROR) from None

        return _run_database(operation)

    # -- teacher review persistence -------------------------------------

    def create(self, record: object) -> None:
        """Implement ``TeacherReviewRepository.create`` transactionally."""

        payload, payload_checksum = _review_payload(record)
        review_id = payload["review_id"]

        def operation() -> None:
            with self._connection() as connection:
                with connection.transaction():
                    existing = connection.execute(
                        """
                        SELECT payload, payload_checksum
                        FROM m3_teacher_reviews
                        WHERE review_id = %s
                        FOR UPDATE
                        """,
                        (review_id,),
                    ).fetchone()
                    if existing is not None:
                        if (
                            _json_value(existing["payload"]) == payload
                            and existing["payload_checksum"] == payload_checksum
                        ):
                            return
                        raise DomainError(
                            code="M3_REVIEW_DUPLICATE",
                            module="m3",
                            message="review identity already exists",
                            recoverable=True,
                        )
                    connection.execute(
                        """
                        INSERT INTO m3_teacher_reviews(
                            review_id, subject_id, input_checksum,
                            validation_report_ref, state, version,
                            reviewer_pseudonym, reason, created_at,
                            updated_at, history, payload, payload_checksum
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                  %s, %s, %s, %s)
                        ON CONFLICT (review_id) DO NOTHING
                        """,
                        (
                            payload["review_id"],
                            payload["subject_id"],
                            payload["input_checksum"],
                            payload["validation_report_ref"],
                            payload["state"],
                            payload["version"],
                            payload["reviewer_pseudonym"],
                            payload["reason"],
                            _parse_datetime(payload["created_at"]),
                            _parse_datetime(payload["updated_at"]),
                            Jsonb(payload["history"]),
                            Jsonb(payload),
                            payload_checksum,
                        ),
                    )

        _run_database(operation)

    def get(self, review_id: str) -> object | None:
        return self.load_teacher_review(review_id)

    def load_teacher_review(self, review_id: str) -> object | None:
        def operation() -> object | None:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT payload, payload_checksum
                    FROM m3_teacher_reviews
                    WHERE review_id = %s
                    """,
                    (review_id,),
                ).fetchone()
            if row is None:
                return None
            payload = _strict_json_object(row["payload"], name="review payload")
            checksum = _sha256(dumps_json(payload).encode("utf-8"))
            if row["payload_checksum"] != checksum:
                raise PostgresOperationError(_INTEGRITY_ERROR)
            return _review_from_payload(payload)

        return _run_database(operation)

    def compare_and_swap(
        self,
        review_id: str,
        expected_version: int,
        record: object,
    ) -> bool:
        payload, payload_checksum = _review_payload(record)
        if payload["review_id"] != review_id:
            raise PostgresOperationError(_INTEGRITY_ERROR)

        def operation() -> bool:
            with self._connection() as connection:
                with connection.transaction():
                    current = connection.execute(
                        """
                        SELECT payload, payload_checksum, version
                        FROM m3_teacher_reviews
                        WHERE review_id = %s
                        FOR UPDATE
                        """,
                        (review_id,),
                    ).fetchone()
                    if current is None or current["version"] != expected_version:
                        return False
                    connection.execute(
                        """
                        UPDATE m3_teacher_reviews
                        SET subject_id = %s,
                            input_checksum = %s,
                            validation_report_ref = %s,
                            state = %s,
                            version = %s,
                            reviewer_pseudonym = %s,
                            reason = %s,
                            created_at = %s,
                            updated_at = %s,
                            history = %s,
                            payload = %s,
                            payload_checksum = %s
                        WHERE review_id = %s AND version = %s
                        """,
                        (
                            payload["subject_id"],
                            payload["input_checksum"],
                            payload["validation_report_ref"],
                            payload["state"],
                            payload["version"],
                            payload["reviewer_pseudonym"],
                            payload["reason"],
                            _parse_datetime(payload["created_at"]),
                            _parse_datetime(payload["updated_at"]),
                            Jsonb(payload["history"]),
                            Jsonb(payload),
                            payload_checksum,
                            review_id,
                            expected_version,
                        ),
                    )
                    return True

        return _run_database(operation)

    def save_teacher_review(self, record: object) -> None:
        self.create(record)

    # Names mirror the infrastructure-facing adapter used by M3's workflow
    # boundary; the shorter methods above remain useful to the native port.
    def get_teacher_review(self, review_id: str) -> object | None:
        return self.load_teacher_review(review_id)

    def compare_and_swap_teacher_review(
        self,
        review_id: str,
        expected_version: int,
        record: object,
    ) -> bool:
        return self.compare_and_swap(review_id, expected_version, record)

    @contextmanager
    def lock_teacher_review(self, review_id: str) -> Iterator[None]:
        """Hold a transaction-scoped lock across review publish callbacks."""

        if not _safe_identity(review_id):
            raise PostgresOperationError("review identity is invalid")

        callback_error: BaseException | None = None
        try:
            with self._connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"course-insight:m3-review:{review_id}",),
                    )
                    try:
                        yield
                    except BaseException as error:
                        # Keep the publisher's exception as the primary error
                        # even if transaction cleanup also reports a driver
                        # failure while rolling it back.
                        callback_error = error
                        raise
        except DomainError:
            raise
        except PostgresError:
            raise
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if callback_error is not None and not isinstance(
                callback_error, psycopg.Error
            ):
                raise callback_error
            raise PostgresConnectionError(
                "PostgreSQL connection is unavailable"
            ) from None
        except psycopg.Error:
            if callback_error is not None and not isinstance(
                callback_error, psycopg.Error
            ):
                raise callback_error
            raise PostgresOperationError(
                "teacher review lock is unavailable"
            ) from None


class PostgresPgVectorStore:
    """Transactional PostgreSQL+pgvector implementation of ``VectorStore``."""

    backend_name = "postgresql+pgvector"

    def __init__(self, pool: object) -> None:
        self._pool = pool

    def assert_available(self) -> None:
        def operation() -> None:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                row = connection.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_extension WHERE extname = 'vector'
                    ) AS enabled
                    """
                ).fetchone()
            if not row or row.get("enabled") is not True:
                raise PostgresOperationError(
                    "pgvector extension is unavailable"
                )

        _run_vector(operation)

    def begin(self, index_id: str, index_version: str, *, dimension: int) -> None:
        _validate_vector_identity(index_id, index_version)
        _validate_dimension(dimension)
        self.assert_available()

        def operation() -> None:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    current = connection.execute(
                        """
                        SELECT dimension, status
                        FROM m2_vector_indexes
                        WHERE index_id = %s AND index_version = %s
                        FOR UPDATE
                        """,
                        (index_id, index_version),
                    ).fetchone()
                    if current is not None and current.get("status") == "ready":
                        raise PostgresOperationError(
                            "ready vector index is immutable"
                        )
                    if current is not None and int(current["dimension"]) != dimension:
                        raise PostgresOperationError(
                            "vector index dimension conflicts"
                        )
                    connection.execute(
                        """
                        DELETE FROM m2_vector_documents
                        WHERE index_id = %s AND index_version = %s
                        """,
                        (index_id, index_version),
                    )
                    connection.execute(
                        """
                        DELETE FROM m2_vector_indexes
                        WHERE index_id = %s AND index_version = %s
                        """,
                        (index_id, index_version),
                    )
                    connection.execute(
                        """
                        INSERT INTO m2_vector_indexes(
                            index_id, index_version, dimension, status
                        ) VALUES (%s, %s, %s, 'staging')
                        """,
                        (index_id, index_version, dimension),
                    )

        _run_vector(operation)

    def add(
        self,
        index_id: str,
        index_version: str,
        document: VectorDocument,
    ) -> None:
        self.add_many(index_id, index_version, [document])

    def add_many(
        self,
        index_id: str,
        index_version: str,
        documents: Sequence[VectorDocument],
    ) -> None:
        """Write one bounded batch in one checkout and one transaction."""

        _validate_vector_identity(index_id, index_version)
        rows = _validated_vector_batch(documents)

        def operation() -> None:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    current = connection.execute(
                        """
                        SELECT dimension, status
                        FROM m2_vector_indexes
                        WHERE index_id = %s AND index_version = %s
                        FOR UPDATE
                        """,
                        (index_id, index_version),
                    ).fetchone()
                    if current is None or current.get("status") != "staging":
                        raise PostgresOperationError("vector index is not ready")
                    dimension = int(current["dimension"])
                    _validate_dimension(dimension)
                    seen: set[str] = set()
                    for document in rows:
                        _validate_vector_document(document, dimension)
                        if document.evidence_id in seen:
                            raise PostgresOperationError("duplicate vector document")
                        seen.add(document.evidence_id)
                    existing = connection.execute(
                        """
                        SELECT evidence_id
                        FROM m2_vector_documents
                        WHERE index_id = %s AND index_version = %s
                          AND evidence_id = ANY(%s)
                        """,
                        (
                            index_id,
                            index_version,
                            [document.evidence_id for document in rows],
                        ),
                    ).fetchall()
                    if existing:
                        raise PostgresOperationError("duplicate vector document")
                    insert_statement = """
                        INSERT INTO m2_vector_documents(
                            index_id, index_version, evidence_id,
                            chunk_id, text_checksum, dimension, embedding
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                        """
                    insert_parameters = [
                        (
                            index_id,
                            index_version,
                            document.evidence_id,
                            document.chunk_id,
                            document.text_checksum,
                            dimension,
                            _vector_literal(document.vector),
                        )
                        for document in rows
                    ]
                    with connection.cursor() as cursor:
                        cursor.executemany(insert_statement, insert_parameters)

        _run_vector(operation)

    def discard(self, index_id: str, index_version: str) -> None:
        """Delete only staging rows; published vector indexes are immutable."""

        _validate_vector_identity(index_id, index_version)

        def operation() -> None:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    connection.execute(
                        """
                        DELETE FROM m2_vector_documents
                        WHERE index_id = %s AND index_version = %s
                          AND EXISTS (
                              SELECT 1
                              FROM m2_vector_indexes
                              WHERE index_id = %s AND index_version = %s
                                AND status = 'staging'
                          )
                        """,
                        (index_id, index_version, index_id, index_version),
                    )
                    connection.execute(
                        """
                        DELETE FROM m2_vector_indexes
                        WHERE index_id = %s AND index_version = %s
                          AND status = 'staging'
                        """,
                        (index_id, index_version),
                    )

        _run_vector(operation)

    def publish(
        self,
        index_id: str,
        index_version: str,
        *,
        expected_count: int,
        checksum: str,
        metadata: VectorIndexMetadata | None = None,
    ) -> None:
        _validate_vector_identity(index_id, index_version)
        if type(expected_count) is not int or expected_count < 1:
            raise PostgresOperationError("vector batch is not complete")
        if not _is_sha256(checksum):
            raise PostgresOperationError("vector checksum is invalid")
        if metadata is not None:
            _validate_vector_metadata(metadata, checksum, expected_count)

        def operation() -> None:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                with connection.transaction():
                    current = connection.execute(
                        """
                        SELECT dimension, status
                        FROM m2_vector_indexes
                        WHERE index_id = %s AND index_version = %s
                        FOR UPDATE
                        """,
                        (index_id, index_version),
                    ).fetchone()
                    count_row = connection.execute(
                        """
                        SELECT COUNT(*) AS count
                        FROM m2_vector_documents
                        WHERE index_id = %s AND index_version = %s
                        """,
                        (index_id, index_version),
                    ).fetchone()
                    if (
                        current is None
                        or current.get("status") != "staging"
                        or count_row is None
                        or int(count_row["count"]) != expected_count
                    ):
                        raise PostgresOperationError("vector batch is not complete")
                    if metadata is None:
                        connection.execute(
                            """
                            UPDATE m2_vector_indexes
                            SET status = 'ready', checksum = %s,
                                chunk_count = %s, published_at = CURRENT_TIMESTAMP
                            WHERE index_id = %s AND index_version = %s
                            """,
                            (checksum, expected_count, index_id, index_version),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE m2_vector_indexes
                            SET status = 'ready', checksum = %s,
                                chunk_count = %s, embedding_model_id = %s,
                                metadata = %s,
                                published_at = CURRENT_TIMESTAMP
                            WHERE index_id = %s AND index_version = %s
                            """,
                            (
                                checksum,
                                expected_count,
                                metadata.embedding_model_id,
                                Jsonb(_vector_metadata_payload(metadata)),
                                index_id,
                                index_version,
                            ),
                        )

        _run_vector(operation)

    def ready(self, index_id: str, index_version: str) -> bool:
        _validate_vector_identity(index_id, index_version)

        def operation() -> bool:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                row = connection.execute(
                    """
                    SELECT status
                    FROM m2_vector_indexes
                    WHERE index_id = %s AND index_version = %s
                    """,
                    (index_id, index_version),
                ).fetchone()
            return bool(row and row.get("status") == "ready")

        return _run_vector(operation)

    def get_metadata(
        self, index_id: str, index_version: str
    ) -> VectorIndexMetadata | None:
        _validate_vector_identity(index_id, index_version)

        def operation() -> VectorIndexMetadata | None:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                row = connection.execute(
                    """
                    SELECT metadata, status, checksum, dimension,
                           chunk_count, embedding_model_id
                    FROM m2_vector_indexes
                    WHERE index_id = %s AND index_version = %s
                    """,
                    (index_id, index_version),
                ).fetchone()
            if row is None or row.get("status") != "ready":
                raise PostgresOperationError("vector index is not ready")
            payload = row.get("metadata")
            if payload is None or payload == {}:
                return None
            metadata = _vector_metadata_from_payload(payload)
            if (
                metadata.index_checksum != row.get("checksum")
                or metadata.dimension != int(row.get("dimension"))
                or metadata.chunk_count != int(row.get("chunk_count"))
                or metadata.embedding_model_id != row.get("embedding_model_id")
            ):
                raise PostgresOperationError(_VECTOR_INTEGRITY_ERROR)
            return metadata

        return _run_vector(operation)

    def search(
        self,
        index_id: str,
        index_version: str,
        query_vector: Sequence[float],
        *,
        top_k: int,
    ) -> tuple[VectorMatch, ...]:
        dimension = self._dimension(index_id, index_version, ready_only=True)
        _validate_vector(query_vector, dimension)
        if type(top_k) is not int or top_k < 1:
            raise PostgresOperationError("vector top-k is invalid")
        vector = _vector_literal(query_vector)
        try:
            search_statement = build_pgvector_exact_search_sql(dimension)
        except ValueError as error:
            raise PostgresOperationError(str(error)) from None

        def operation() -> tuple[VectorMatch, ...]:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                rows = connection.execute(
                    search_statement,
                    (vector, index_id, index_version, vector, top_k),
                ).fetchall()
            matches: list[VectorMatch] = []
            for row in rows:
                score = float(row["score"])
                if not math.isfinite(score):
                    raise PostgresOperationError(_VECTOR_INTEGRITY_ERROR)
                matches.append(
                    VectorMatch(
                        evidence_id=str(row["evidence_id"]),
                        chunk_id=str(row["chunk_id"]),
                        score=score,
                        text_checksum=str(row["text_checksum"]),
                    )
                )
            return tuple(matches)

        return _run_vector(operation)

    def _dimension(
        self,
        index_id: str,
        index_version: str,
        *,
        staging_only: bool = False,
        ready_only: bool = False,
    ) -> int:
        _validate_vector_identity(index_id, index_version)

        def operation() -> int:
            with self._pool.connection() as connection:  # type: ignore[attr-defined]
                row = connection.execute(
                    """
                    SELECT dimension, status
                    FROM m2_vector_indexes
                    WHERE index_id = %s AND index_version = %s
                    """,
                    (index_id, index_version),
                ).fetchone()
            if row is None or row.get("status") not in {"staging", "ready"}:
                raise PostgresOperationError("vector index is not ready")
            if staging_only and row.get("status") != "staging":
                raise PostgresOperationError("ready vector index is immutable")
            if ready_only and row.get("status") != "ready":
                raise PostgresOperationError("vector index is not ready")
            dimension = int(row["dimension"])
            _validate_dimension(dimension)
            return dimension

        return _run_vector(operation)


def _manifest_value(manifest: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
    if isinstance(manifest, Mapping):
        return dict(manifest)
    if isinstance(manifest, str):
        manifest = manifest.encode("utf-8")
    if type(manifest) is not bytes:
        raise ValueError("manifest must be a mapping or JSON document")
    return _strict_json_object(manifest, name="repository manifest")


def _identity(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record["module"]),
        str(record["object_type"]),
        str(record["object_id"]),
        str(record["object_version"]),
    )


def _validate_vector_identity(index_id: str, index_version: str) -> None:
    if any(
        not _safe_identity(value) or "/" in value or "\\" in value
        for value in (index_id, index_version)
    ):
        raise PostgresOperationError("vector index identity is invalid")


def _validate_dimension(dimension: object) -> None:
    if (
        type(dimension) is not int
        or dimension < 1
        or dimension > MAX_PGVECTOR_DIMENSION
    ):
        raise PostgresOperationError("vector dimension is invalid")


def _validate_vector(vector: Sequence[float], dimension: int) -> None:
    if len(vector) != dimension or any(
        type(value) not in {int, float} or not math.isfinite(float(value))
        for value in vector
    ):
        raise PostgresOperationError("vector is invalid")


def _validate_vector_document(document: VectorDocument, dimension: int) -> None:
    if not isinstance(document, VectorDocument):
        raise PostgresOperationError("vector document is invalid")
    _validate_vector(document.vector, dimension)
    if any(
        not _safe_identity(value)
        for value in (document.evidence_id, document.chunk_id)
    ):
        raise PostgresOperationError("vector document is invalid")
    if not _is_sha256(document.text_checksum):
        raise PostgresOperationError("vector document checksum is invalid")


def _validated_vector_batch(
    documents: Sequence[VectorDocument],
) -> tuple[VectorDocument, ...]:
    if not isinstance(documents, Sequence):
        raise PostgresOperationError("vector batch is invalid")
    rows = tuple(documents)
    if not 1 <= len(rows) <= MAX_VECTOR_BATCH_SIZE:
        raise PostgresOperationError(
            f"vector batch size must be between 1 and {MAX_VECTOR_BATCH_SIZE}"
        )
    return rows


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(format(float(value), ".17g") for value in vector) + "]"


def _vector_metadata_payload(metadata: VectorIndexMetadata) -> dict[str, Any]:
    _validate_vector_metadata(metadata, metadata.index_checksum, metadata.chunk_count)
    return {
        "index_checksum": metadata.index_checksum,
        "course_package_id": metadata.course_package_id,
        "course_package_checksum": metadata.course_package_checksum,
        "embedding_model_id": metadata.embedding_model_id,
        "dimension": metadata.dimension,
        "source_count": metadata.source_count,
        "chunk_count": metadata.chunk_count,
        "built_at": metadata.built_at.astimezone(timezone.utc).isoformat(),
    }


def _vector_metadata_from_payload(payload: object) -> VectorIndexMetadata:
    if not isinstance(payload, Mapping):
        raise PostgresOperationError(_VECTOR_INTEGRITY_ERROR)
    try:
        metadata = VectorIndexMetadata(
            index_checksum=str(payload["index_checksum"]),
            course_package_id=str(payload["course_package_id"]),
            course_package_checksum=str(payload["course_package_checksum"]),
            embedding_model_id=str(payload["embedding_model_id"]),
            dimension=int(payload["dimension"]),
            source_count=int(payload["source_count"]),
            chunk_count=int(payload["chunk_count"]),
            built_at=_parse_datetime(payload["built_at"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PostgresOperationError(_VECTOR_INTEGRITY_ERROR) from None
    _validate_vector_metadata(metadata, metadata.index_checksum, metadata.chunk_count)
    return metadata


def _validate_vector_metadata(
    metadata: VectorIndexMetadata,
    checksum: str,
    expected_count: int,
) -> None:
    if not isinstance(metadata, VectorIndexMetadata):
        raise PostgresOperationError("vector metadata is invalid")
    if (
        not _is_sha256(metadata.index_checksum)
        or not _is_sha256(metadata.course_package_checksum)
        or metadata.index_checksum != checksum
        or not _safe_identity(metadata.course_package_id)
        or not _safe_identity(metadata.embedding_model_id)
        or type(metadata.dimension) is not int
        or metadata.dimension < 1
        or metadata.dimension > 16_000
        or type(metadata.source_count) is not int
        or metadata.source_count < 1
        or type(metadata.chunk_count) is not int
        or metadata.chunk_count != expected_count
        or metadata.built_at.tzinfo is None
        or metadata.built_at.utcoffset() is None
    ):
        raise PostgresOperationError("vector metadata is invalid")


def _validate_retrieval_audit_payload(payload: object) -> None:
    if type(payload) is not dict or set(payload) != {"audit", "metadata"}:
        raise ValueError("audit payload is invalid")
    metadata = payload["metadata"]
    if type(metadata) is not dict or set(metadata) != {
        "embedding_model_id",
        "index_version",
        "latency_ms",
        "policy_version",
        "query_checksum",
        "request_id",
        "retrieved_scores",
    }:
        raise ValueError("audit metadata is invalid")
    if not _is_sha256(metadata["query_checksum"]):
        raise ValueError("audit query checksum is invalid")
    if type(metadata["retrieved_scores"]) is not list:
        raise ValueError("audit scores are invalid")
    if type(metadata["latency_ms"]) is not int or metadata["latency_ms"] < 0:
        raise ValueError("audit latency is invalid")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("datetime is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _review_payload(record: object) -> tuple[dict[str, Any], str]:
    from course_insight.modules.m3_knowledge_bundle.teacher_review import (
        TeacherReviewAction,
        TeacherReviewRecord,
    )

    if not isinstance(record, TeacherReviewRecord):
        raise DomainError(
            code="M3_REVIEW_INVALID",
            module="m3",
            message="review payload is invalid",
        )
    if (
        not _safe_identity(record.review_id)
        or not _safe_identity(record.subject_id)
        or not _safe_identity(record.validation_report_ref)
        or not _is_sha256(record.input_checksum)
        or record.state not in {"draft", "submitted", "approved", "rejected", "recalled"}
        or type(record.version) is not int
        or record.version < 1
        or type(record.history) is not tuple
        or not record.history
    ):
        raise DomainError(
            code="M3_REVIEW_INVALID",
            module="m3",
            message="review payload is invalid",
        )
    actions: list[dict[str, Any]] = []
    expected_transitions = {
        "draft": {"submitted"},
        "submitted": {"approved", "rejected"},
        "approved": {"recalled"},
        "rejected": {"recalled"},
        "recalled": set(),
    }
    previous_state: str | None = None
    for expected_version, action in enumerate(record.history, start=1):
        if not isinstance(action, TeacherReviewAction):
            raise DomainError(
                code="M3_REVIEW_INVALID",
                module="m3",
                message="review payload is invalid",
            )
        if (
            action.state not in expected_transitions
            or type(action.version) is not int
            or action.version != expected_version
            or action.occurred_at.tzinfo is None
            or action.occurred_at.utcoffset() is None
            or action.reviewer_pseudonym is not None
            and not _safe_review_text(action.reviewer_pseudonym, max_length=128)
            or action.reason is not None
            and not _safe_review_text(action.reason, max_length=2000)
        ):
            raise DomainError(
                code="M3_REVIEW_INVALID",
                module="m3",
                message="review payload is invalid",
            )
        if previous_state is None:
            if action.state != "draft":
                raise DomainError(
                    code="M3_REVIEW_INVALID",
                    module="m3",
                    message="review payload is invalid",
                )
        elif action.state not in expected_transitions[previous_state]:
            raise DomainError(
                code="M3_REVIEW_INVALID",
                module="m3",
                message="review payload is invalid",
            )
        previous_state = action.state
        actions.append(
            {
                "state": action.state,
                "reviewer_pseudonym": action.reviewer_pseudonym,
                "reason": action.reason,
                "occurred_at": action.occurred_at.astimezone(timezone.utc).isoformat(),
                "version": action.version,
            }
        )
    for value in (record.created_at, record.updated_at):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DomainError(
                code="M3_REVIEW_INVALID",
                module="m3",
                message="review payload is invalid",
            )
    if (
        len(record.history) != record.version
        or previous_state != record.state
        or record.reviewer_pseudonym != record.history[-1].reviewer_pseudonym
        or record.reason != record.history[-1].reason
        or record.created_at != record.history[0].occurred_at
        or record.updated_at != record.history[-1].occurred_at
        or record.updated_at < record.created_at
        or record.reviewer_pseudonym is not None
        and not _safe_review_text(record.reviewer_pseudonym, max_length=128)
        or record.reason is not None
        and not _safe_review_text(record.reason, max_length=2000)
    ):
        raise DomainError(
            code="M3_REVIEW_INVALID",
            module="m3",
            message="review payload is invalid",
        )
    payload = {
        "created_at": record.created_at.astimezone(timezone.utc).isoformat(),
        "history": actions,
        "input_checksum": record.input_checksum,
        "reason": record.reason,
        "review_id": record.review_id,
        "reviewer_pseudonym": record.reviewer_pseudonym,
        "state": record.state,
        "subject_id": record.subject_id,
        "updated_at": record.updated_at.astimezone(timezone.utc).isoformat(),
        "validation_report_ref": record.validation_report_ref,
        "version": record.version,
    }
    payload_bytes = dumps_json(payload).encode("utf-8")
    return payload, _sha256(payload_bytes)


def _safe_review_text(value: object, *, max_length: int) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and value == value.strip()
        and len(value) <= max_length
        and "\x00" not in value
    )


def _review_from_payload(payload: Mapping[str, Any]) -> object:
    from course_insight.modules.m3_knowledge_bundle.teacher_review import (
        TeacherReviewAction,
        TeacherReviewRecord,
    )

    required = {
        "created_at",
        "history",
        "input_checksum",
        "reason",
        "review_id",
        "reviewer_pseudonym",
        "state",
        "subject_id",
        "updated_at",
        "validation_report_ref",
        "version",
    }
    if set(payload) != required or type(payload["history"]) is not list:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    try:
        history = tuple(
            TeacherReviewAction(
                state=item["state"],
                reviewer_pseudonym=item["reviewer_pseudonym"],
                reason=item["reason"],
                occurred_at=_parse_datetime(item["occurred_at"]),
                version=item["version"],
            )
            for item in payload["history"]
        )
        record = TeacherReviewRecord(
            review_id=payload["review_id"],
            subject_id=payload["subject_id"],
            input_checksum=payload["input_checksum"],
            validation_report_ref=payload["validation_report_ref"],
            state=payload["state"],
            version=payload["version"],
            reviewer_pseudonym=payload["reviewer_pseudonym"],
            reason=payload["reason"],
            created_at=_parse_datetime(payload["created_at"]),
            updated_at=_parse_datetime(payload["updated_at"]),
            history=history,
        )
        _review_payload(record)
        return record
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


PostgreSQLM1M2M3Repository = PostgresM1M2M3Repository
PostgresS1S6Repository = PostgresM1M2M3Repository
PostgresVectorStore = PostgresPgVectorStore
PgVectorStore = PostgresPgVectorStore


__all__ = [
    "PgVectorStore",
    "PostgresM1M2M3Repository",
    "PostgresOperationError",
    "PostgresPgVectorStore",
    "PostgresS1S6Repository",
    "PostgresVectorStore",
    "PostgreSQLM1M2M3Repository",
]
