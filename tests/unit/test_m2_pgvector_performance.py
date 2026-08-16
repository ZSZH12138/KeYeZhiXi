"""TDD coverage for the M2 PostgreSQL+pgvector performance gate."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import builtins
import json
from typing import Any, Iterator

import pytest

import course_insight.modules.m2_evidence_retrieval.performance as performance
from course_insight.modules.m2_evidence_retrieval.performance import (
    BenchmarkConfig,
    BenchmarkCleanupError,
    BenchmarkConfigurationError,
    BenchmarkIdentity,
    DisposableDatabaseTarget,
    MAX_CHUNK_COUNT,
    MAX_DIMENSION,
    MAX_QUERY_COUNT,
    MAX_TOP_K,
    MAX_WORK_UNITS,
    M2PgVectorBenchmark,
    build_report,
    build_search_sql,
    cleanup_benchmark_index,
    cosine_score,
    explain_search,
    generate_vector_batches,
    generate_queries,
    generate_vectors,
    percentile_ms,
    recall_at_k,
    require_numpy,
    serialize_report,
    evaluate_thresholds,
    require_disposable_test_database_url,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    VectorMatch,
    build_pgvector_exact_search_sql,
)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return None if not self._rows else self._rows[0]

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Connection:
    def __init__(self, *, database_name: str = "course_insight_test_ci") -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.database_name = database_name

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> _Cursor:
        self.executions.append((statement, tuple(parameters)))
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select current_database()"):
            return _Cursor([{"database_name": self.database_name}])
        if normalized.startswith("explain"):
            return _Cursor(
                [
                    {
                        "QUERY PLAN": [
                            {
                                "Plan": {
                                    "Node Type": "Index Scan",
                                    "Actual Total Time": 1.25,
                                    "Shared Hit Blocks": 3,
                                    "Shared Read Blocks": 1,
                                }
                            },
                            {"Planning Time": 0.15, "Execution Time": 1.4},
                        ]
                    }
                ]
            )
        if normalized.startswith("delete from m2_vector_"):
            return _Cursor([])
        raise AssertionError(f"unhandled SQL: {statement}")


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection_value = connection

    @contextmanager
    def connection(self) -> Iterator[_Connection]:
        yield self.connection_value


def _config(**overrides: Any) -> BenchmarkConfig:
    values: dict[str, Any] = {
        "chunk_count": 8,
        "dimension": 3,
        "query_count": 4,
        "top_k": 2,
        "max_p95_ms": 50.0,
        "min_recall_at_k": 1.0,
        "seed": 7,
    }
    values.update(overrides)
    return BenchmarkConfig(**values)


def test_percentile_ms_uses_deterministic_interpolation() -> None:
    assert percentile_ms([1.0, 2.0, 3.0, 4.0], 50.0) == 2.5
    assert percentile_ms([1.0, 2.0, 3.0, 4.0], 95.0) == pytest.approx(3.85)


def test_percentile_ms_rejects_empty_or_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        percentile_ms([], 50.0)
    with pytest.raises(ValueError, match="percentile"):
        percentile_ms([1.0], 101.0)


def test_recall_at_k_counts_distinct_hits() -> None:
    assert recall_at_k(["e1", "e2"], ["e1", "e3"], 2) == 0.5
    assert recall_at_k(["e1", "e1"], ["e1", "e2"], 2) == 0.5


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("chunk_count", 0, "chunk_count"),
        ("chunk_count", MAX_CHUNK_COUNT + 1, "chunk_count"),
        ("dimension", 0, "dimension"),
        ("dimension", MAX_DIMENSION + 1, "dimension"),
        ("query_count", 0, "query_count"),
        ("query_count", MAX_QUERY_COUNT + 1, "query_count"),
        ("top_k", 0, "top_k"),
        ("top_k", MAX_TOP_K + 1, "top_k"),
        ("max_p95_ms", 0.0, "max_p95_ms"),
        ("min_recall_at_k", -0.01, "min_recall_at_k"),
        ("min_recall_at_k", 1.01, "min_recall_at_k"),
        ("seed", -1, "seed"),
    ],
)
def test_benchmark_config_rejects_out_of_bounds_values(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        BenchmarkConfig(**{**asdict(_config()), field: value})


def test_benchmark_config_rejects_top_k_larger_than_chunk_count() -> None:
    with pytest.raises(ValueError, match="top_k"):
        _config(chunk_count=2, top_k=3)


def test_target_scale_is_allowed_but_extreme_workload_is_rejected() -> None:
    target = BenchmarkConfig(
        chunk_count=50_000,
        dimension=1_536,
        query_count=100,
        top_k=10,
    )
    assert target.chunk_count * target.query_count * target.dimension <= MAX_WORK_UNITS

    with pytest.raises(ValueError, match="workload"):
        BenchmarkConfig(
            chunk_count=MAX_CHUNK_COUNT,
            dimension=MAX_DIMENSION,
            query_count=MAX_QUERY_COUNT,
            top_k=1,
        )


def test_benchmark_config_rejects_unbounded_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        _config(batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        _config(batch_size=10_000)


def test_generated_vectors_and_queries_are_float32_quantized() -> None:
    numpy = require_numpy()
    config = _config(chunk_count=4, dimension=5, query_count=3)
    _, vector_batch = next(generate_vector_batches(config, batch_size=2))
    assert vector_batch.dtype == numpy.dtype("float32")
    vectors = numpy.asarray(
        tuple(vector for _, vector in generate_vectors(config)),
        dtype=numpy.float64,
    )
    queries = numpy.asarray(generate_queries(config), dtype=numpy.float64)
    assert numpy.array_equal(vectors, numpy.asarray(vectors, dtype=numpy.float32))
    assert numpy.array_equal(queries, numpy.asarray(queries, dtype=numpy.float32))


def test_exact_ground_truth_matches_scalar_reference_and_is_batch_invariant() -> None:
    config = _config(chunk_count=5, dimension=4, query_count=3, top_k=2, batch_size=2)
    queries = generate_queries(config)
    actual = performance._exact_ground_truth(config, queries)

    vectors = tuple(generate_vectors(config))
    expected = tuple(
        tuple(
            evidence_id
            for _, evidence_id in sorted(
                (
                    (cosine_score(vector, query), evidence_id)
                    for evidence_id, vector in vectors
                ),
                key=lambda item: (-item[0], item[1]),
            )[: config.top_k]
        )
        for query in queries
    )
    assert actual == expected
    assert actual == performance._exact_ground_truth(
        replace(config, batch_size=3),
        queries,
    )


def test_exact_ground_truth_tie_breaks_by_evidence_id_across_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numpy = require_numpy()
    config = _config(chunk_count=4, dimension=2, query_count=1, top_k=2, batch_size=2)
    batches = (
        (0, numpy.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=numpy.float32)),
        (2, numpy.asarray([[1.0, 0.0], [-1.0, 0.0]], dtype=numpy.float32)),
    )
    monkeypatch.setattr(
        performance,
        "generate_vector_batches",
        lambda config: iter(batches),
    )

    assert performance._exact_ground_truth(config, ((1.0, 0.0),)) == (
        (
            "m2-benchmark-evidence-0000000000",
            "m2-benchmark-evidence-0000000002",
        ),
    )


def test_performance_and_adapter_exact_sql_use_one_builder() -> None:
    assert build_search_sql(3) == build_pgvector_exact_search_sql(3)
    assert build_search_sql(3, explain=True) == build_pgvector_exact_search_sql(
        3,
        explain=True,
    )


def test_same_seed_replays_vectors_and_queries() -> None:
    config = _config()
    assert tuple(generate_vectors(config)) == tuple(generate_vectors(config))
    assert generate_queries(config) == generate_queries(config)
    assert tuple(generate_vectors(config)) != tuple(generate_vectors(replace(config, seed=8)))


def test_threshold_evaluation_reports_pass_and_each_failed_gate() -> None:
    config = _config(max_p95_ms=20.0, min_recall_at_k=0.9)
    assert evaluate_thresholds(20.0, 0.9, config) == (True, ())

    passed, reasons = evaluate_thresholds(20.01, 0.89, config)
    assert passed is False
    assert reasons == (
        "p95_ms exceeds max_p95_ms",
        "recall_at_k is below min_recall_at_k",
    )


def test_report_has_stable_pass_fail_json_schema() -> None:
    config = _config()
    report = build_report(
        config=config,
        status="passed",
        environment={"database_guard": "passed", "database_name": "test_ci"},
        identity=BenchmarkIdentity("m2_benchmark_7_1", "v1"),
        build_ms=12.5,
        p50_ms=1.5,
        p95_ms=2.5,
        recall_at_k_value=1.0,
        relation_bytes={"heap_bytes": 10, "table_bytes": 20, "total_relation_bytes": 40},
        index_bytes={"existing": [], "benchmark_ann_index_bytes": None},
        explain={"summary": {}, "plan": []},
        failure_reasons=(),
    )
    assert report["passed"] is True
    assert report["failure_reasons"] == []
    assert {
        "schema_version",
        "status",
        "environment",
        "scale",
        "build_ms",
        "p50_ms",
        "p95_ms",
        "recall_at_k",
        "relation_bytes",
        "index_bytes",
        "explain",
        "thresholds",
        "passed",
        "failure_reasons",
    } <= set(report)

    failed = build_report(
        config=config,
        status="failed",
        environment={"database_guard": "passed", "database_name": "test_ci"},
        identity=BenchmarkIdentity("m2_benchmark_7_2", "v1"),
        build_ms=None,
        p50_ms=None,
        p95_ms=25.0,
        recall_at_k_value=0.8,
        relation_bytes={"heap_bytes": None, "table_bytes": None, "total_relation_bytes": None},
        index_bytes={"existing": [], "benchmark_ann_index_bytes": None},
        explain=None,
        failure_reasons=("p95_ms exceeds max_p95_ms",),
    )
    assert failed["passed"] is False
    assert failed["failure_reasons"] == ["p95_ms exceeds max_p95_ms"]
    assert json.loads(serialize_report(failed)) == failed


def test_explain_search_is_parameterized_and_returns_plan_summary() -> None:
    connection = _Connection()
    result = explain_search(
        _Pool(connection),
        BenchmarkIdentity("m2_benchmark_7_3", "v1"),
        dimension=3,
        query_vector=(1.0, 0.0, 0.0),
        top_k=2,
    )

    assert result["summary"] == {
        "root_node_type": "Index Scan",
        "planning_ms": 0.15,
        "execution_ms": 1.4,
        "shared_hit_blocks": 3,
        "shared_read_blocks": 1,
        "shared_dirtied_blocks": 0,
        "shared_written_blocks": 0,
    }
    statement, parameters = connection.executions[0]
    assert "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)" in statement
    assert "embedding::vector(3) <=> %s::vector" in statement
    assert "ORDER BY embedding::vector(3) <=> %s::vector" in statement
    assert parameters[1:3] == ("m2_benchmark_7_3", "v1")
    assert parameters[-1] == 2


def test_cleanup_deletes_only_the_benchmark_identity() -> None:
    connection = _Connection()
    cleanup_benchmark_index(
        _Pool(connection),
        _target(),
        BenchmarkIdentity("m2_benchmark_7_4", "v1"),
    )

    assert len(connection.executions) == 3
    assert connection.executions[0][0].lower().startswith("select current_database()")
    assert [statement.lower().split()[0:3] for statement, _ in connection.executions[1:]] == [
        ["delete", "from", "m2_vector_documents"],
        ["delete", "from", "m2_vector_indexes"],
    ]
    assert connection.executions[0][1] == ()
    assert all(
        parameters == ("m2_benchmark_7_4", "v1")
        for _, parameters in connection.executions[1:]
    )
    assert all("drop table" not in statement.lower() for statement, _ in connection.executions)


def test_cleanup_database_mismatch_fails_before_any_delete() -> None:
    connection = _Connection(database_name="another_test_ci")
    with pytest.raises(BenchmarkConfigurationError, match="does not match"):
        cleanup_benchmark_index(
            _Pool(connection),
            _target(),
            BenchmarkIdentity("m2_benchmark_7_9", "v1"),
        )
    assert len(connection.executions) == 1
    assert all("delete" not in statement.lower() for statement, _ in connection.executions)


def test_disposable_guard_rejects_reserved_database_name() -> None:
    with pytest.raises(BenchmarkConfigurationError, match="reserved"):
        require_disposable_test_database_url(
            {
                "COURSE_INSIGHT_TEST_DATABASE_URL": (
                    "postgresql://tester:secret@db.internal/postgres"
                ),
                "COURSE_INSIGHT_TEST_DATABASE_NAME": "postgres",
            }
        )


def test_disposable_target_constructor_is_the_core_guard() -> None:
    target = DisposableDatabaseTarget(
        "postgresql://tester:secret@db.internal/course_insight_test_ci",
        "course_insight_test_ci",
    )
    assert target.database_name == "course_insight_test_ci"
    with pytest.raises(BenchmarkConfigurationError, match="test, ci, or tmp"):
        DisposableDatabaseTarget(
            "postgresql://tester:secret@db.internal/course_insight_prod",
            "course_insight_prod",
        )


def test_cli_malformed_ipv6_dsn_is_blocked_without_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.benchmark_m2_pgvector import main

    malformed_dsn = (
        "postgresql://secret-user:secret-pass@[2001:db8::1/"
        "course_insight_test_ci"
    )
    monkeypatch.setenv("COURSE_INSIGHT_TEST_DATABASE_URL", malformed_dsn)
    monkeypatch.setenv("COURSE_INSIGHT_TEST_DATABASE_NAME", "course_insight_test_ci")

    exit_code = main(["--chunk-count", "2", "--dimension", "2", "--top-k", "1"])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert report["status"] == "blocked"
    assert report["failure_reasons"] == ["the PostgreSQL test DSN is malformed"]
    assert malformed_dsn not in captured.out


class _RunStore:
    def __init__(self, *, retrieved_id: str = "e0", failure: Exception | None = None) -> None:
        self.retrieved_id = retrieved_id
        self.failure = failure
        self.batches: list[tuple[Any, ...]] = []
        self.begun = False
        self.published = False

    def assert_available(self) -> None:
        return None

    def begin(self, index_id: str, index_version: str, *, dimension: int) -> None:
        self.begun = True

    def add_many(
        self,
        index_id: str,
        index_version: str,
        documents: tuple[Any, ...],
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.batches.append(tuple(documents))

    def publish(
        self,
        index_id: str,
        index_version: str,
        *,
        expected_count: int,
        checksum: str,
    ) -> None:
        self.published = True

    def search(
        self,
        index_id: str,
        index_version: str,
        query_vector: tuple[float, ...],
        *,
        top_k: int,
    ) -> tuple[VectorMatch, ...]:
        if self.failure is not None:
            raise self.failure
        return (VectorMatch(self.retrieved_id, "c0", 1.0, "0" * 64),)


def _target() -> DisposableDatabaseTarget:
    return DisposableDatabaseTarget(
        "postgresql://tester:secret@db.internal/course_insight_test_ci",
        "course_insight_test_ci",
    )


def _patch_benchmark_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cleanup: Any,
) -> None:
    monkeypatch.setattr(
        performance,
        "read_environment",
        lambda pool, target: {
            "database_guard": "passed",
            "database_name": target.database_name,
            "backend": "postgresql+pgvector",
        },
    )
    monkeypatch.setattr(
        performance,
        "_exact_ground_truth",
        lambda config, queries: tuple(("e0",) for _ in queries),
    )
    monkeypatch.setattr(
        performance,
        "explain_search",
        lambda *args, **kwargs: {"summary": {}, "plan": []},
    )
    monkeypatch.setattr(
        performance,
        "collect_storage_bytes",
        lambda pool: (
            {"heap_bytes": 1, "table_bytes": 2, "total_relation_bytes": 3},
            {"existing": [], "benchmark_ann_index_created": False},
        ),
    )
    monkeypatch.setattr(performance, "cleanup_benchmark_index", cleanup)


def _clock() -> Any:
    current = 0

    def tick() -> int:
        nonlocal current
        current += 1_000_000
        return current

    return tick


def test_benchmark_run_success_uses_bounded_add_many_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RunStore()
    cleanups: list[BenchmarkIdentity] = []
    _patch_benchmark_dependencies(
        monkeypatch,
        cleanup=lambda pool, target, identity: cleanups.append(identity),
    )
    report = M2PgVectorBenchmark(
        object(),
        _config(chunk_count=5, query_count=2, top_k=1, batch_size=2),
        target=_target(),
        identity=BenchmarkIdentity("m2_benchmark_7_5", "v1"),
        store_factory=lambda pool: store,
        clock_ns=_clock(),
    ).run()

    assert report["passed"] is True
    assert [len(batch) for batch in store.batches] == [2, 2, 1]
    assert store.published is True
    assert len(cleanups) == 1


def test_benchmark_run_returns_failed_report_when_threshold_is_not_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RunStore(retrieved_id="wrong")
    _patch_benchmark_dependencies(
        monkeypatch,
        cleanup=lambda pool, target, identity: None,
    )
    report = M2PgVectorBenchmark(
        object(),
        _config(chunk_count=3, query_count=2, top_k=1),
        target=_target(),
        identity=BenchmarkIdentity("m2_benchmark_7_6", "v1"),
        store_factory=lambda pool: store,
        clock_ns=_clock(),
    ).run()

    assert report["status"] == "failed"
    assert report["passed"] is False
    assert "recall_at_k is below min_recall_at_k" in report["failure_reasons"]


@pytest.mark.parametrize("failure_at", ["write", "query"])
def test_benchmark_run_propagates_write_or_query_error_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    store = _RunStore(failure=RuntimeError(f"{failure_at} failure"))
    cleanups: list[BenchmarkIdentity] = []
    _patch_benchmark_dependencies(
        monkeypatch,
        cleanup=lambda pool, target, identity: cleanups.append(identity),
    )
    if failure_at == "query":
        original_search = store.search
        store.failure = None

        def fail_search(*args: Any, **kwargs: Any) -> tuple[VectorMatch, ...]:
            raise RuntimeError("query failure")

        store.search = fail_search  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match=failure_at):
        M2PgVectorBenchmark(
            object(),
            _config(chunk_count=3, query_count=2, top_k=1),
            target=_target(),
            identity=BenchmarkIdentity("m2_benchmark_7_7", "v1"),
            store_factory=lambda pool: store,
            clock_ns=_clock(),
        ).run()
    assert len(cleanups) == 1


def test_benchmark_run_does_not_swallow_cleanup_error_or_primary_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RunStore(failure=RuntimeError("write failure"))

    def fail_cleanup(
        pool: object,
        target: DisposableDatabaseTarget,
        identity: BenchmarkIdentity,
    ) -> None:
        raise RuntimeError("cleanup failure")

    _patch_benchmark_dependencies(monkeypatch, cleanup=fail_cleanup)
    with pytest.raises(BenchmarkCleanupError) as raised:
        M2PgVectorBenchmark(
            object(),
            _config(chunk_count=3, query_count=1, top_k=1),
            target=_target(),
            identity=BenchmarkIdentity("m2_benchmark_7_8", "v1"),
            store_factory=lambda pool: store,
            clock_ns=_clock(),
        ).run()
    assert isinstance(raised.value.primary_error, RuntimeError)
    assert raised.value.failure_reasons == (
        "PostgreSQL benchmark execution failed",
        "cleanup_failed",
    )


def test_benchmark_requires_verified_target_object() -> None:
    with pytest.raises(BenchmarkConfigurationError, match="DisposableDatabaseTarget"):
        M2PgVectorBenchmark(
            object(),
            _config(),
            target="course_insight_test_ci",  # type: ignore[arg-type]
        )


def test_benchmark_does_not_cleanup_when_environment_guard_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_calls: list[tuple[Any, ...]] = []

    def reject_environment(
        pool: object,
        target: DisposableDatabaseTarget,
    ) -> dict[str, object]:
        raise BenchmarkConfigurationError("connected database does not match")

    monkeypatch.setattr(performance, "read_environment", reject_environment)
    monkeypatch.setattr(
        performance,
        "cleanup_benchmark_index",
        lambda *args: cleanup_calls.append(args),
    )
    with pytest.raises(BenchmarkConfigurationError, match="does not match"):
        M2PgVectorBenchmark(
            object(),
            _config(),
            target=_target(),
            identity=BenchmarkIdentity("m2_benchmark_7_10", "v1"),
            store_factory=lambda pool: pytest.fail("store must not be constructed"),
        ).run()
    assert cleanup_calls == []


def test_benchmark_logic_failure_cleanup_error_preserves_built_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _RunStore(retrieved_id="wrong")

    def fail_cleanup(
        pool: object,
        target: DisposableDatabaseTarget,
        identity: BenchmarkIdentity,
    ) -> None:
        raise RuntimeError("cleanup transport detail")

    _patch_benchmark_dependencies(monkeypatch, cleanup=fail_cleanup)
    with pytest.raises(BenchmarkCleanupError) as raised:
        M2PgVectorBenchmark(
            object(),
            _config(chunk_count=3, query_count=2, top_k=1),
            target=_target(),
            identity=BenchmarkIdentity("m2_benchmark_7_11", "v1"),
            store_factory=lambda pool: store,
            clock_ns=_clock(),
        ).run()

    report = raised.value.report
    assert report is not None
    assert report["status"] == "failed"
    assert report["passed"] is False
    assert report["build_ms"] is not None
    assert report["p50_ms"] is not None
    assert report["p95_ms"] is not None
    assert report["recall_at_k"] == 0.0
    assert report["explain"] == {"summary": {}, "plan": []}
    assert "recall_at_k is below min_recall_at_k" in report["failure_reasons"]
    assert "cleanup_failed" in report["failure_reasons"]


def test_require_numpy_normalizes_ordinary_exception_without_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def fail_numpy(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "numpy":
            raise OSError("DLL ABI secret detail")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_numpy)
    with pytest.raises(BenchmarkConfigurationError, match="numpy>=2,<3") as raised:
        require_numpy()
    assert "DLL" not in str(raised.value)
    assert "ABI" not in str(raised.value)


def test_require_numpy_does_not_swallow_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def interrupt_numpy(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "numpy":
            raise KeyboardInterrupt
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", interrupt_numpy)
    with pytest.raises(KeyboardInterrupt):
        require_numpy()


def test_cli_without_guard_emits_blocked_json_and_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.benchmark_m2_pgvector import main

    monkeypatch.delenv("COURSE_INSIGHT_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("COURSE_INSIGHT_TEST_DATABASE_NAME", raising=False)

    exit_code = main(["--chunk-count", "2", "--dimension", "2", "--top-k", "1"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code != 0
    assert report["status"] == "blocked"
    assert report["passed"] is False
    assert "COURSE_INSIGHT_TEST_DATABASE_URL" in report["failure_reasons"][0]


def test_cli_missing_numpy_is_blocked_and_does_not_expose_dsn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.benchmark_m2_pgvector as cli

    secret_dsn = "postgresql://secret-user:secret-pass@private.internal/course_insight_test_ci"
    monkeypatch.setenv("COURSE_INSIGHT_TEST_DATABASE_URL", secret_dsn)
    monkeypatch.setenv("COURSE_INSIGHT_TEST_DATABASE_NAME", "course_insight_test_ci")

    def missing_numpy() -> None:
        raise BenchmarkConfigurationError("numpy>=2,<3 is required for the benchmark")

    monkeypatch.setattr(cli, "require_numpy", missing_numpy)
    exit_code = cli.main(["--chunk-count", "2", "--dimension", "2", "--top-k", "1"])
    output = capsys.readouterr().out
    report = json.loads(output)

    assert exit_code == 2
    assert report["status"] == "blocked"
    assert "numpy>=2,<3" in report["failure_reasons"][0]
    assert secret_dsn not in output


def test_cli_execution_failure_is_redacted_and_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.benchmark_m2_pgvector as cli

    secret_dsn = "postgresql://secret-user:secret-pass@private.internal/course_insight_test_ci"
    monkeypatch.setenv("COURSE_INSIGHT_TEST_DATABASE_URL", secret_dsn)
    monkeypatch.setenv("COURSE_INSIGHT_TEST_DATABASE_NAME", "course_insight_test_ci")
    monkeypatch.setattr(
        cli,
        "create_postgres_pool",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(secret_dsn)),
    )
    exit_code = cli.main(["--chunk-count", "2", "--dimension", "2", "--top-k", "1"])
    output = capsys.readouterr().out
    report = json.loads(output)

    assert exit_code == 1
    assert report["status"] == "failed"
    assert secret_dsn not in output
    assert report["failure_reasons"] == ["PostgreSQL benchmark execution failed"]


def test_cli_unknown_argument_is_redacted_and_returns_blocked_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.benchmark_m2_pgvector import main

    monkeypatch.delenv("COURSE_INSIGHT_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("COURSE_INSIGHT_TEST_DATABASE_NAME", raising=False)
    secret = "--unknown=postgresql://secret-user:secret-pass@private.internal/prod"

    exit_code = main([secret])
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert secret not in captured.out
    assert report["status"] == "blocked"
    assert report["failure_reasons"] == ["invalid benchmark arguments"]
