"""TDD coverage for the PostgreSQL S1-S6 artifact and pgvector adapters."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
import pytest

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.m1_m2_m3_repository import (
    PostgresM1M2M3Repository,
    PostgresPgVectorStore,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    VectorDocument,
    VectorIndexMetadata,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    create_retrieval_audit,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    TeacherReviewWorkflow,
)
from tests.unit.test_s1_s6_persistence import (
    _bundle_artifact,
    _course_import,
    _index_artifact,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = [] if rows is None else rows

    def fetchone(self) -> dict[str, Any] | None:
        return None if not self._rows else deepcopy(self._rows[0])

    def fetchall(self) -> list[dict[str, Any]]:
        return deepcopy(self._rows)


class _Connection:
    """Small stateful fake that exercises the adapter's actual SQL boundary."""

    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.vector_indexes: dict[tuple[str, str], dict[str, Any]] = {}
        self.vector_documents: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.audits: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_entries = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_on: str | None = None
        self._in_transaction = False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        snapshot = (
            deepcopy(self.artifacts),
            deepcopy(self.vector_indexes),
            deepcopy(self.vector_documents),
            deepcopy(self.audits),
            deepcopy(self.reviews),
        )
        self.transaction_entries += 1
        self._in_transaction = True
        try:
            yield
        except Exception:
            (
                self.artifacts,
                self.vector_indexes,
                self.vector_documents,
                self.audits,
                self.reviews,
            ) = snapshot
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
        finally:
            self._in_transaction = False

    def execute(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> _Cursor:
        normalized = " ".join(statement.lower().split())
        parameters = tuple(parameters)
        self.executions.append((statement, parameters))
        if self.fail_on is not None and self.fail_on.lower() in normalized:
            raise psycopg.DataError("private SQL detail")

        if "from m1_m2_m3_artifacts" in normalized:
            if "where" in normalized:
                key = tuple(str(value) for value in parameters[:4])
                row = self.artifacts.get(key)
                return _Cursor([] if row is None else [row])
            return _Cursor(
                [
                    self.artifacts[key]
                    for key in sorted(self.artifacts)
                ]
            )
        if normalized.startswith("insert into m1_m2_m3_artifacts"):
            values = parameters
            key = tuple(str(value) for value in values[:4])
            self.artifacts.setdefault(
                key,
                {
                    "module": values[0],
                    "object_type": values[1],
                    "object_id": values[2],
                    "object_version": values[3],
                    "status": values[4],
                    "content_checksum": values[5],
                    "payload_version": values[6],
                    "payload_checksum": values[7],
                    "payload": _json_value(values[8]),
                },
            )
            return _Cursor()

        if "m2_retrieval_audits" in normalized:
            if normalized.startswith("insert into"):
                self.audits[str(parameters[0])] = {
                    "audit_id": parameters[0],
                    "query_id": parameters[1],
                    "index_id": parameters[2],
                    "index_version": parameters[3],
                    "policy_id": parameters[4],
                    "status": parameters[5],
                    "created_at": parameters[6],
                    "payload": _json_value(parameters[7]),
                    "payload_checksum": parameters[8],
                }
                return _Cursor()
            row = self.audits.get(str(parameters[0]))
            return _Cursor([] if row is None else [row])

        if "m3_teacher_reviews" in normalized:
            if normalized.startswith("insert into"):
                payload = _json_value(parameters[11])
                self.reviews[str(parameters[0])] = {
                    "review_id": parameters[0],
                    "version": parameters[5],
                    "payload": payload,
                    "payload_checksum": parameters[12],
                }
                return _Cursor()
            if normalized.startswith("update"):
                row = self.reviews[str(parameters[12])]
                row.update(
                    version=parameters[4],
                    payload=_json_value(parameters[10]),
                    payload_checksum=parameters[11],
                )
                return _Cursor()
            row = self.reviews.get(str(parameters[0]))
            return _Cursor([] if row is None else [row])

        if "from m2_vector_indexes" in normalized:
            key = (str(parameters[0]), str(parameters[1]))
            row = self.vector_indexes.get(key)
            return _Cursor([] if row is None else [row])
        if normalized.startswith("delete from m2_vector_documents"):
            index_id, index_version = str(parameters[0]), str(parameters[1])
            for key in list(self.vector_documents):
                if key[:2] == (index_id, index_version):
                    del self.vector_documents[key]
            return _Cursor()
        if normalized.startswith("delete from m2_vector_indexes"):
            self.vector_indexes.pop((str(parameters[0]), str(parameters[1])), None)
            return _Cursor()
        if normalized.startswith("insert into m2_vector_indexes"):
            key = (str(parameters[0]), str(parameters[1]))
            self.vector_indexes[key] = {
                "index_id": key[0],
                "index_version": key[1],
                "dimension": int(parameters[2]),
                "status": "staging",
                "checksum": None,
                "chunk_count": 0,
                "metadata": None,
                "embedding_model_id": None,
            }
            return _Cursor()
        if normalized.startswith("insert into m2_vector_documents"):
            key = (
                str(parameters[0]),
                str(parameters[1]),
                str(parameters[2]),
            )
            self.vector_documents[key] = {
                "index_id": key[0],
                "index_version": key[1],
                "evidence_id": key[2],
                "chunk_id": parameters[3],
                "text_checksum": parameters[4],
                "dimension": parameters[5],
                "embedding": parameters[6],
            }
            return _Cursor()
        if normalized.startswith("select count(*)"):
            index_id, index_version = str(parameters[0]), str(parameters[1])
            count = sum(
                key[:2] == (index_id, index_version)
                for key in self.vector_documents
            )
            return _Cursor([{"count": count}])
        if normalized.startswith("update m2_vector_indexes"):
            if "metadata =" in normalized:
                index_id, index_version = str(parameters[4]), str(parameters[5])
            else:
                index_id, index_version = str(parameters[2]), str(parameters[3])
            row = self.vector_indexes[(index_id, index_version)]
            row.update(
                status="ready",
                checksum=parameters[0],
                chunk_count=parameters[1],
            )
            if "metadata =" in normalized:
                row["embedding_model_id"] = parameters[2]
                row["metadata"] = _json_value(parameters[3])
            return _Cursor()
        if normalized.startswith("select exists"):
            return _Cursor([{"enabled": True}])
        if "from m2_vector_documents" in normalized and "order by" in normalized:
            index_id, index_version = str(parameters[1]), str(parameters[2])
            rows = [
                {
                    "evidence_id": row["evidence_id"],
                    "chunk_id": row["chunk_id"],
                    "text_checksum": row["text_checksum"],
                    "score": 0.75,
                }
                for key, row in self.vector_documents.items()
                if key[:2] == (index_id, index_version)
            ]
            return _Cursor(rows)
        raise AssertionError(f"unhandled SQL: {statement}")


def _json_value(value: Any) -> Any:
    return getattr(value, "obj", value)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value


class _FailingPool:
    def __init__(self, error: Exception) -> None:
        self.error = error

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        raise self.error
        yield  # pragma: no cover


def test_postgres_repository_round_trips_complete_file_codec_artifacts(
    tmp_path: Any,
) -> None:
    package, course_snapshot = _course_import()
    index, lexical_snapshot = _index_artifact(package)
    bundle, report, seed = _bundle_artifact(tmp_path, package)
    connection = _Connection()
    repository = PostgresM1M2M3Repository(_Pool(connection))

    repository.save_course_import(package, course_snapshot)
    repository.save_index_artifact(index, lexical_snapshot)
    repository.save_bundle_artifact(bundle, report, seed)

    assert repository.load_course_import(
        package.course_package_id, package.package_version
    ) == (package, course_snapshot)
    assert repository.load_index_artifact(index.index_id, index.index_version) == (
        index,
        lexical_snapshot,
    )
    assert repository.load_bundle_artifact(
        bundle.knowledge_bundle_id, bundle.bundle_version
    ) == (bundle, report, seed)
    manifest = repository.export_manifest()
    assert manifest["format"] == "s1_s6_repository_manifest/v1"
    assert len(manifest["records"]) == 3
    assert len(manifest["manifest_checksum"]) == 64


def test_postgres_repository_is_idempotent_for_same_checksum_and_rejects_conflict(
    tmp_path: Any,
) -> None:
    package, snapshot = _course_import()
    connection = _Connection()
    repository = PostgresM1M2M3Repository(_Pool(connection))

    repository.save_course_import(package, snapshot)
    repository.save_course_import(package.model_copy(deep=True), snapshot)
    conflicting, conflicting_snapshot = _course_import(text="changed")
    with pytest.raises(PostgresOperationError, match="conflict"):
        repository.save_course_import(conflicting, conflicting_snapshot)
    assert len(connection.artifacts) == 1


def test_import_manifest_validates_every_record_before_one_transaction(
    tmp_path: Any,
) -> None:
    package, snapshot = _course_import()
    source_connection = _Connection()
    source = PostgresM1M2M3Repository(_Pool(source_connection))
    source.save_course_import(package, snapshot)
    manifest = source.export_manifest()

    target_connection = _Connection()
    target = PostgresM1M2M3Repository(_Pool(target_connection))
    assert target.import_manifest(manifest) == 1
    assert target.import_manifest(manifest) == 1

    target_connection.fail_on = "from m1_m2_m3_artifacts"
    with pytest.raises(PostgresOperationError, match="operation failed"):
        target.import_manifest(manifest)
    assert target_connection.rollbacks == 1


def test_postgres_errors_are_preserved_or_sanitized_at_adapter_boundary() -> None:
    connection_error = PostgresConnectionError("PostgreSQL connection is unavailable")
    repository = PostgresM1M2M3Repository(_FailingPool(connection_error))
    with pytest.raises(PostgresConnectionError) as captured:
        repository.export_manifest()
    assert captured.value is connection_error

    connection = _Connection()
    connection.fail_on = "from m1_m2_m3_artifacts"
    unsafe = PostgresM1M2M3Repository(_Pool(connection))
    with pytest.raises(PostgresOperationError, match="operation failed") as captured:
        unsafe.export_manifest()
    assert "private SQL detail" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_pgvector_adapter_publishes_complete_batch_and_uses_parameterized_search() -> None:
    connection = _Connection()
    store = PostgresPgVectorStore(_Pool(connection))
    store.begin("index_1", "v1", dimension=2)
    store.add(
        "index_1",
        "v1",
        VectorDocument("e1", "chunk_1", (1.0, 0.0), "a" * 64),
    )
    store.publish("index_1", "v1", expected_count=1, checksum="b" * 64)

    assert store.ready("index_1", "v1")
    matches = store.search("index_1", "v1", (1.0, 0.0), top_k=1)
    assert matches[0].evidence_id == "e1"
    assert any(
        "%s::vector" in statement and "ORDER BY" in statement
        for statement, _ in connection.executions
    )


def test_pgvector_adapter_does_not_silently_accept_incomplete_batches() -> None:
    connection = _Connection()
    store = PostgresPgVectorStore(_Pool(connection))
    store.begin("index_1", "v1", dimension=2)
    with pytest.raises(PostgresOperationError, match="complete"):
        store.publish("index_1", "v1", expected_count=1, checksum="b" * 64)


def test_pgvector_adapter_persists_restart_bindings_with_ready_index() -> None:
    connection = _Connection()
    store = PostgresPgVectorStore(_Pool(connection))
    store.begin("index_1", "v1", dimension=2)
    store.add(
        "index_1",
        "v1",
        VectorDocument("e1", "chunk_1", (1.0, 0.0), "a" * 64),
    )
    metadata = VectorIndexMetadata(
        index_checksum="b" * 64,
        course_package_id="course-package-1",
        course_package_checksum="c" * 64,
        embedding_model_id="openai_compatible:model:v1:2",
        dimension=2,
        source_count=1,
        chunk_count=1,
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    store.publish(
        "index_1",
        "v1",
        expected_count=1,
        checksum="b" * 64,
        metadata=metadata,
    )

    assert store.get_metadata("index_1", "v1") == metadata
    assert connection.vector_indexes[("index_1", "v1")]["embedding_model_id"] == (
        metadata.embedding_model_id
    )


def test_postgres_metadata_ports_round_trip_redacted_audits_and_cas_reviews() -> None:
    from tests.unit.test_retrieval_audit import _index, _policy, _query

    connection = _Connection()
    repository = PostgresM1M2M3Repository(_Pool(connection))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    envelope = create_retrieval_audit(
        query=_query(),
        index=_index(),
        policy=_policy(),
        status="succeeded",
        evidence_ids=["evidence-1"],
        scores=[0.75],
        latency_ms=12,
        request_id="request-1",
        created_at=now,
    )
    repository.save_retrieval_audit(envelope)
    repository.save_retrieval_audit(envelope)
    assert repository.load_retrieval_audit(envelope.audit.audit_id) == envelope

    workflow = TeacherReviewWorkflow(repository)  # type: ignore[arg-type]
    draft = workflow.create_draft(
        review_id="review-1",
        subject_id="course-1",
        input_checksum="a" * 64,
        validation_report_ref="report-1",
        now=now,
    )
    submitted = workflow.submit(
        "review-1", "teacher-1", "checked", draft.version, now
    )
    assert repository.get("review-1") == submitted
