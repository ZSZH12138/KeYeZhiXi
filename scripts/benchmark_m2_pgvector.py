"""Run the bounded M2 PostgreSQL+pgvector performance acceptance gate."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys

from course_insight.infrastructure.postgresql.migration_runner import run_migrations
from course_insight.infrastructure.postgresql.pool import create_postgres_pool
from course_insight.modules.m2_evidence_retrieval.performance import (
    BenchmarkCleanupError,
    BenchmarkConfig,
    BenchmarkConfigurationError,
    BenchmarkIdentity,
    DEFAULT_CHUNK_COUNT,
    DEFAULT_DIMENSION,
    DEFAULT_MAX_P95_MS,
    DEFAULT_MIN_RECALL_AT_K,
    DEFAULT_QUERY_COUNT,
    DEFAULT_SEED,
    DEFAULT_TOP_K,
    DEFAULT_BATCH_SIZE,
    M2PgVectorBenchmark,
    build_report,
    make_benchmark_identity,
    failure_reasons_for_error,
    require_disposable_test_database_url,
    require_numpy,
    serialize_report,
)


class _BenchmarkArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        """Reject parser errors without echoing user-controlled arguments."""

        raise BenchmarkConfigurationError("invalid benchmark arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _BenchmarkArgumentParser(
        description=(
            "Measure exact PostgreSQL+pgvector M2 retrieval on a disposable "
            "test database; no ANN index is created."
        )
    )
    parser.add_argument(
        "--chunk-count",
        default=str(DEFAULT_CHUNK_COUNT),
        help=f"synthetic chunk count (default: {DEFAULT_CHUNK_COUNT})",
    )
    parser.add_argument(
        "--dimension",
        default=str(DEFAULT_DIMENSION),
        help=f"validated vector dimension (default: {DEFAULT_DIMENSION})",
    )
    parser.add_argument(
        "--query-count",
        default=str(DEFAULT_QUERY_COUNT),
        help=f"measured query count (default: {DEFAULT_QUERY_COUNT})",
    )
    parser.add_argument(
        "--top-k",
        default=str(DEFAULT_TOP_K),
        help=f"retrieval top-k (default: {DEFAULT_TOP_K})",
    )
    parser.add_argument(
        "--max-p95-ms",
        default=str(DEFAULT_MAX_P95_MS),
        help=f"maximum accepted P95 in milliseconds (default: {DEFAULT_MAX_P95_MS})",
    )
    parser.add_argument(
        "--min-recall-at-k",
        default=str(DEFAULT_MIN_RECALL_AT_K),
        help=(
            "minimum accepted recall at k, between 0 and 1 "
            f"(default: {DEFAULT_MIN_RECALL_AT_K})"
        ),
    )
    parser.add_argument(
        "--seed",
        default=str(DEFAULT_SEED),
        help=f"deterministic vector/query seed (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--batch-size",
        default=str(DEFAULT_BATCH_SIZE),
        help=f"bounded write batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except BenchmarkConfigurationError as error:
        report = _failure_report(
            BenchmarkConfig(),
            status="blocked",
            environment={"database_guard": "not_checked"},
            identity=BenchmarkIdentity("m2_benchmark_0_0", "v1"),
            reason=str(error),
        )
        _write_report(report)
        return 2
    try:
        config = BenchmarkConfig(
            chunk_count=_parse_int(args.chunk_count, "chunk_count"),
            dimension=_parse_int(args.dimension, "dimension"),
            query_count=_parse_int(args.query_count, "query_count"),
            top_k=_parse_int(args.top_k, "top_k"),
            max_p95_ms=_parse_float(args.max_p95_ms, "max_p95_ms"),
            min_recall_at_k=_parse_float(
                args.min_recall_at_k,
                "min_recall_at_k",
            ),
            seed=_parse_int(args.seed, "seed"),
            batch_size=_parse_int(args.batch_size, "batch_size"),
        )
    except (TypeError, ValueError) as error:
        report = _failure_report(
            BenchmarkConfig(),
            status="blocked",
            environment={"database_guard": "not_checked"},
            identity=BenchmarkIdentity("m2_benchmark_0_0", "v1"),
            reason=str(error),
        )
        _write_report(report)
        return 2

    try:
        target = require_disposable_test_database_url()
    except BenchmarkConfigurationError as error:
        report = _failure_report(
            config,
            status="blocked",
            environment={
                "database_guard": "failed",
                "backend": "postgresql+pgvector",
            },
            identity=BenchmarkIdentity("m2_benchmark_0_0", "v1"),
            reason=str(error),
        )
        _write_report(report)
        return 2

    try:
        require_numpy()
    except BenchmarkConfigurationError as error:
        report = _failure_report(
            config,
            status="blocked",
            environment={
                "database_guard": "passed",
                "database_name": target.database_name,
                "backend": "postgresql+pgvector",
            },
            identity=BenchmarkIdentity("m2_benchmark_0_0", "v1"),
            reason=str(error),
        )
        _write_report(report)
        return 2

    database_url = target.dsn
    database_name = target.database_name
    identity = make_benchmark_identity(config.seed)
    environment = {
        "database_guard": "passed",
        "database_name": database_name,
        "backend": "postgresql+pgvector",
    }
    pool = None
    report: dict[str, object] | None = None
    try:
        pool = create_postgres_pool(
            database_url,
            min_size=1,
            max_size=2,
            connect_timeout_seconds=10.0,
        )
        run_migrations(pool)
        report = M2PgVectorBenchmark(
            pool,
            config,
            target=target,
            identity=identity,
        ).run()
    except BenchmarkCleanupError as error:
        report = error.report or _failure_report(
            config,
            status="failed",
            environment=environment,
            identity=identity,
            reasons=failure_reasons_for_error(error),
        )
    except BenchmarkConfigurationError as error:
        report = _failure_report(
            config,
            status="blocked",
            environment=environment,
            identity=identity,
            reason=str(error),
        )
    except Exception as error:
        report = _failure_report(
            config,
            status="failed",
            environment=environment,
            identity=identity,
            reasons=failure_reasons_for_error(error),
        )
    finally:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                if report is None:
                    report = _failure_report(
                        config,
                        status="failed",
                        environment=environment,
                        identity=identity,
                        reason="cleanup_failed",
                    )
                else:
                    report = dict(report)
                    report["status"] = "failed"
                    report["passed"] = False
                    reasons = list(report.get("failure_reasons", []))
                    reasons.append("cleanup_failed")
                    report["failure_reasons"] = reasons

    assert report is not None
    _write_report(report)
    return 0 if report["passed"] is True else 2 if report["status"] == "blocked" else 1


def _parse_int(value: object, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an integer") from error


def _parse_float(value: object, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a number") from error


def _failure_report(
    config: BenchmarkConfig,
    *,
    status: str,
    environment: dict[str, object],
    identity: BenchmarkIdentity,
    reason: str | None = None,
    reasons: Sequence[str] | None = None,
) -> dict[str, object]:
    failure_reasons = tuple(reasons) if reasons is not None else ((reason,) if reason else ())
    return build_report(
        config=config,
        status=status,
        environment=environment,
        identity=identity,
        build_ms=None,
        p50_ms=None,
        p95_ms=None,
        recall_at_k_value=None,
        relation_bytes={
            "heap_bytes": None,
            "table_bytes": None,
            "total_relation_bytes": None,
        },
        index_bytes={
            "existing": [],
            "benchmark_ann_index_created": False,
            "benchmark_ann_index_name": None,
            "benchmark_ann_index_bytes": None,
        },
        explain=None,
        failure_reasons=failure_reasons,
    )


def _write_report(report: dict[str, object]) -> None:
    sys.stdout.write(serialize_report(report) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
