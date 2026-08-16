"""TDD coverage for the PostgreSQL S1-S6 artifact and pgvector adapters."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
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
from course_insight.contracts.errors import DomainError
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    MAX_PGVECTOR_DIMENSION,
    VectorDocument,
    VectorIndexMetadata,
    build_pgvector_exact_search_sql,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    create_retrieval_audit,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    RepositoryTeacherReviewRepository,
    TeacherReviewAction,
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


class _BatchCursor:
    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection

    def executemany(
        self,
        statement: str,
        parameters: list[tuple[Any, ...]],
    ) -> None:
        self._connection.executemany(statement, parameters)


class _Connection:
    """Small stateful fake that exercises the adapter's actual SQL boundary."""

    def __init__(self) -> None:
        self.artifacts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.vector_indexes: dict[tuple[str, str], dict[str, Any]] = {}
        self.vector_documents: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.audits: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, dict[str, Any]] = {}
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.executemany_calls: list[tuple[str, tuple[tuple[Any, ...], ...]]] = []
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
        if "from m2_vector_documents" in normalized and "= any" in normalized:
            index_id, index_version = str(parameters[0]), str(parameters[1])
            wanted = {str(value) for value in parameters[2]}
            return _Cursor(
                [
                    {"evidence_id": row["evidence_id"]}
                    for key, row in self.vector_documents.items()
                    if key[:2] == (index_id, index_version)
                    and key[2] in wanted
                ]
            )
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

    @contextmanager
    def cursor(self) -> Iterator[_BatchCursor]:
        yield _BatchCursor(self)

    def executemany(
        self,
        statement: str,
        parameters: list[tuple[Any, ...]],
    ) -> None:
        batch = tuple(tuple(values) for values in parameters)
        self.executemany_calls.append((statement, batch))
        for values in batch:
            self.execute(statement, values)


def _json_value(value: Any) -> Any:
    return getattr(value, "obj", value)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value


class _SingleCheckoutConnection(_Connection):
    def __init__(self) -> None:
        super().__init__()
        self.lock_active = False
        self.lock_active_during_execute: list[bool] = []
        self.review_read_lock_states: list[bool] = []
        self.transaction_depth = 0
        self.savepoint_rollbacks = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        snapshot = (
            deepcopy(self.artifacts),
            deepcopy(self.vector_indexes),
            deepcopy(self.vector_documents),
            deepcopy(self.audits),
            deepcopy(self.reviews),
        )
        outermost = self.transaction_depth == 0
        self.transaction_depth += 1
        self.transaction_entries += 1
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
            if outermost:
                self.rollbacks += 1
            else:
                self.savepoint_rollbacks += 1
            raise
        else:
            if outermost:
                self.commits += 1
        finally:
            self.transaction_depth -= 1
            if outermost:
                self.lock_active = False

    def execute(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> _Cursor:
        normalized = " ".join(statement.lower().split())
        if "pg_advisory_xact_lock" in normalized:
            self.executions.append((statement, tuple(parameters)))
            self.lock_active = True
            self.lock_active_during_execute.append(self.lock_active)
            return _Cursor()
        if normalized.startswith("select") and "from m3_teacher_reviews" in normalized:
            self.review_read_lock_states.append(self.lock_active)
        return super().execute(statement, parameters)

    def reset_observations(self) -> None:
        self.executions.clear()
        self.executemany_calls.clear()
        self.lock_active_during_execute.clear()
        self.review_read_lock_states.clear()


class _SingleCheckoutPool:
    def __init__(self) -> None:
        self.connection_value = _SingleCheckoutConnection()
        self.active_checkouts = 0
        self.checkout_count = 0
        self.max_active_checkouts = 0

    @contextmanager
    def connection(self) -> Iterator[_SingleCheckoutConnection]:
        if self.active_checkouts != 0:
            raise AssertionError("pool checkout would deadlock")
        self.active_checkouts += 1
        self.checkout_count += 1
        self.max_active_checkouts = max(
            self.max_active_checkouts,
            self.active_checkouts,
        )
        try:
            yield self.connection_value
        finally:
            self.active_checkouts -= 1

    def reset_observations(self) -> None:
        self.checkout_count = 0
        self.max_active_checkouts = 0
        self.connection_value.reset_observations()


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


def test_teacher_review_lock_maps_connection_failures_without_driver_details() -> None:
    repository = PostgresM1M2M3Repository(
        _FailingPool(psycopg.OperationalError("dsn=secret host=private"))
    )

    with pytest.raises(PostgresConnectionError) as captured:
        with repository.lock_teacher_review("review-connection"):
            pass

    assert "dsn=secret" not in str(captured.value)


@pytest.mark.parametrize(
    "original_error",
    [
        DomainError(
            code="M3_PUBLISH_REJECTED",
            module="m3",
            message="publisher rejected the bundle",
        ),
        RuntimeError("publisher failed outside the repository boundary"),
        ValueError("publisher value rejected"),
    ],
    ids=["domain-error", "runtime-error", "value-error"],
)
def test_teacher_review_publish_reuses_one_checkout_and_preserves_callback_error(
    original_error: Exception,
) -> None:
    pool = _SingleCheckoutPool()
    repository = PostgresM1M2M3Repository(pool)
    setup_workflow = TeacherReviewWorkflow(repository)  # type: ignore[arg-type]
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    draft = setup_workflow.create_draft(
        review_id="review-lock",
        subject_id="course-lock",
        input_checksum="a" * 64,
        validation_report_ref="report-lock",
        now=now,
    )
    submitted = setup_workflow.submit(
        "review-lock", "teacher-lock", "checked", draft.version, now
    )
    approved = setup_workflow.approve(
        "review-lock", "teacher-lock", "approved", submitted.version, now
    )
    workflow = TeacherReviewWorkflow(RepositoryTeacherReviewRepository(repository))
    temporary_record = replace(
        approved,
        state="recalled",
        version=approved.version + 1,
        reviewer_pseudonym="teacher-lock",
        reason="temporary callback write",
        updated_at=now,
        history=(
            *approved.history,
            TeacherReviewAction(
                state="recalled",
                reviewer_pseudonym="teacher-lock",
                reason="temporary callback write",
                occurred_at=now,
                version=approved.version + 1,
            ),
        ),
    )
    callback_lock_states: list[bool] = []

    def failing_publisher() -> object:
        callback_lock_states.append(pool.connection_value.lock_active)
        # The pool-keyed repository may be initialized again by a cached
        # composition root while the lock callback is active.
        reused_repository = PostgresM1M2M3Repository(pool)
        assert reused_repository.get_teacher_review("review-lock") == approved
        assert reused_repository.compare_and_swap_teacher_review(
            "review-lock", approved.version, temporary_record
        )
        raise original_error

    pool.reset_observations()
    if isinstance(original_error, DomainError):
        with pytest.raises(DomainError) as captured:
            workflow.publish_approved(
                "review-lock",
                failing_publisher,
                expected_version=approved.version,
            )
    elif isinstance(original_error, RuntimeError):
        with pytest.raises(RuntimeError) as captured:
            workflow.publish_approved(
                "review-lock",
                failing_publisher,
                expected_version=approved.version,
            )
    else:
        with pytest.raises(ValueError) as captured:
            workflow.publish_approved(
                "review-lock",
                failing_publisher,
                expected_version=approved.version,
            )

    assert captured.value is original_error
    assert callback_lock_states == [True]
    assert pool.active_checkouts == 0
    assert pool.checkout_count == 1
    assert pool.max_active_checkouts == 1
    assert pool.connection_value.lock_active is False
    assert pool.connection_value.lock_active_during_execute == [True]
    # Approved check, callback read, and the CAS row-lock read all stay under
    # the advisory-lock transaction.
    assert pool.connection_value.review_read_lock_states == [True, True, True]
    assert pool.connection_value.rollbacks == 1
    assert pool.connection_value.reviews["review-lock"]["version"] == approved.version
    lock_calls = [
        (statement, parameters)
        for statement, parameters in pool.connection_value.executions
        if "pg_advisory_xact_lock" in statement
    ]
    assert lock_calls
    assert all("%s" in statement for statement, _ in lock_calls)
    assert all(
        parameters == ("course-insight:m3-review:review-lock",)
        for _, parameters in lock_calls
    )


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
        "%s::vector" in statement
        and "embedding::vector(2)" in statement
        and "ORDER BY" in statement
        for statement, _ in connection.executions
    )
    expected_sql = " ".join(build_pgvector_exact_search_sql(2).split())
    assert any(
        " ".join(statement.split()) == expected_sql
        for statement, _ in connection.executions
    )


def test_postgres_pgvector_begin_rejects_dimension_before_checkout() -> None:
    pool = _SingleCheckoutPool()
    store = PostgresPgVectorStore(pool)

    with pytest.raises(PostgresOperationError, match="vector dimension"):
        store.begin(
            "index_1",
            "v1",
            dimension=MAX_PGVECTOR_DIMENSION + 1,
        )

    assert pool.checkout_count == 0


def test_pgvector_add_many_uses_one_checkout_and_rolls_back_validation_failure() -> None:
    pool = _SingleCheckoutPool()
    store = PostgresPgVectorStore(pool)
    store.begin("index_1", "v1", dimension=2)
    pool.reset_observations()
    transaction_entries_before = pool.connection_value.transaction_entries

    store.add_many(
        "index_1",
        "v1",
        [
            VectorDocument("e1", "chunk_1", (1.0, 0.0), "a" * 64),
            VectorDocument("e2", "chunk_2", (0.0, 1.0), "b" * 64),
        ],
    )
    assert pool.checkout_count == 1
    assert pool.connection_value.transaction_entries == transaction_entries_before + 1
    assert len(pool.connection_value.executemany_calls) == 1
    assert len(pool.connection_value.executemany_calls[0][1]) == 2
    assert len(pool.connection_value.vector_documents) == 2

    store.add_many(
        "index_1",
        "v1",
        [VectorDocument("e3", "chunk_3", (1.0, 0.0), "c" * 64)],
    )
    assert len(pool.connection_value.executemany_calls) == 2
    assert len(pool.connection_value.executemany_calls[1][1]) == 1

    pool.reset_observations()
    with pytest.raises(PostgresOperationError, match="duplicate vector document"):
        store.add_many(
            "index_1",
            "v1",
            [VectorDocument("e1", "chunk_1-again", (1.0, 0.0), "d" * 64)],
        )
    assert pool.checkout_count == 1
    assert pool.connection_value.executemany_calls == []

    pool.reset_observations()
    transaction_entries_before = pool.connection_value.transaction_entries
    rollbacks_before = pool.connection_value.rollbacks
    with pytest.raises(PostgresOperationError, match="vector is invalid"):
        store.add_many(
            "index_1",
            "v1",
            [
                VectorDocument("e3", "chunk_3", (1.0, 0.0), "c" * 64),
                VectorDocument("e4", "chunk_4", (0.0,), "d" * 64),
            ],
        )
    assert pool.checkout_count == 1
    assert pool.connection_value.transaction_entries == transaction_entries_before + 1
    assert pool.connection_value.rollbacks == rollbacks_before + 1
    assert set(pool.connection_value.vector_documents) == {
        ("index_1", "v1", "e1"),
        ("index_1", "v1", "e2"),
        ("index_1", "v1", "e3"),
    }


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
