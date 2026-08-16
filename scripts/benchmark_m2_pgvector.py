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
    BenchmarkProfile,
    M2PgVectorBenchmark,
    TARGET_SCALE_PROFILE,
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
        "--profile",
        choices=("bounded", "target"),
        default="bounded",
        help="named governed scale profile (default: bounded)",
    )
    parser.add_argument(
        "--chunk-count",
        default=None,
        help=f"synthetic chunk count (default: {DEFAULT_CHUNK_COUNT})",
    )
    parser.add_argument(
        "--dimension",
        default=None,
        help=f"validated vector dimension (default: {DEFAULT_DIMENSION})",
    )
    parser.add_argument(
        "--query-count",
        default=None,
        help=f"measured query count (default: {DEFAULT_QUERY_COUNT})",
    )
    parser.add_argument(
        "--top-k",
        default=None,
        help=f"retrieval top-k (default: {DEFAULT_TOP_K})",
    )
    parser.add_argument(
        "--max-p95-ms",
        default=None,
        help=f"maximum accepted P95 in milliseconds (default: {DEFAULT_MAX_P95_MS})",
    )
    parser.add_argument(
        "--min-recall-at-k",
        default=None,
        help=(
            "minimum accepted recall at k, between 0 and 1 "
            f"(default: {DEFAULT_MIN_RECALL_AT_K})"
        ),
    )
    parser.add_argument(
        "--seed",
        default=None,
        help=f"deterministic vector/query seed (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--batch-size",
        default=None,
        help=f"bounded write batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> BenchmarkConfig:
    """Resolve CLI overrides against a named scale profile."""

    if args.profile == "target":
        profile = TARGET_SCALE_PROFILE
    else:
        profile = BenchmarkProfile(
            profile_id="m2-bounded-v1",
            chunk_count=DEFAULT_CHUNK_COUNT,
            dimension=DEFAULT_DIMENSION,
            query_count=DEFAULT_QUERY_COUNT,
            top_k=DEFAULT_TOP_K,
            max_p95_ms=DEFAULT_MAX_P95_MS,
            min_recall_at_k=DEFAULT_MIN_RECALL_AT_K,
            batch_size=DEFAULT_BATCH_SIZE,
        )
    return BenchmarkConfig(
        chunk_count=_parse_int(
            profile.chunk_count if args.chunk_count is None else args.chunk_count,
            "chunk_count",
        ),
        dimension=_parse_int(
            profile.dimension if args.dimension is None else args.dimension,
            "dimension",
        ),
        query_count=_parse_int(
            profile.query_count if args.query_count is None else args.query_count,
            "query_count",
        ),
        top_k=_parse_int(profile.top_k if args.top_k is None else args.top_k, "top_k"),
        max_p95_ms=_parse_float(
            profile.max_p95_ms if args.max_p95_ms is None else args.max_p95_ms,
            "max_p95_ms",
        ),
        min_recall_at_k=_parse_float(
            profile.min_recall_at_k
            if args.min_recall_at_k is None
            else args.min_recall_at_k,
            "min_recall_at_k",
        ),
        seed=_parse_int(
            DEFAULT_SEED if args.seed is None else args.seed,
            "seed",
        ),
        batch_size=_parse_int(
            profile.batch_size if args.batch_size is None else args.batch_size,
            "batch_size",
        ),
        profile_id=profile.profile_id,
        ann_enabled=profile.ann_enabled,
    )


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
        config = config_from_args(args)
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
