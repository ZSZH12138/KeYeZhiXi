"""Safe command-line entry point for explicit SQLite imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from course_insight.infrastructure.config import (
    ConfigurationError,
    load_platform_settings,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    run_migrations,
)
from course_insight.infrastructure.postgresql.pool import create_postgres_pool
from course_insight.infrastructure.postgresql.sqlite_import import (
    MigrationError,
    PostgresImportDestination,
    SQLiteToPostgresMigrator,
)


class _DryRunDestination:
    def apply_batch(self, table: str, rows: tuple[Any, ...]) -> None:
        del table, rows
        raise RuntimeError("dry-run destination must never receive writes")

    def verify_batch(self, table: str, rows: tuple[Any, ...]) -> None:
        del table, rows
        raise RuntimeError("dry-run destination must never receive reads")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and explicitly import SQLite data into PostgreSQL.",
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help=(
            "Durable apply checkpoint (default: <report>.checkpoint.json)."
        ),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root containing config/app.json (default: cwd).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows per independently committed batch (1-10000).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate only (the default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly apply after a successful validation pass.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    values = os.environ if environment is None else environment
    try:
        validation = SQLiteToPostgresMigrator(
            source_path=arguments.source,
            destination=_DryRunDestination(),
        ).run(
            mode="dry-run",
            batch_size=arguments.batch_size,
            report_path=arguments.report,
        )
        if not arguments.apply:
            _print_summary(validation)
            return 0
        try:
            settings = load_platform_settings(
                project_root=arguments.project_root,
                environment=values,
            )
        except ConfigurationError:
            _print_error("MIGRATION_CONFIGURATION_INVALID")
            return 2
        database = settings.database
        if database.backend != "postgresql" or database.url is None:
            _print_error("MIGRATION_CONFIGURATION_INVALID")
            return 2
        database_url = database.url.get_secret_value()
        checkpoint_path = arguments.checkpoint or Path(
            f"{arguments.report}.checkpoint.json"
        )
        pool = create_postgres_pool(
            database_url,
            min_size=database.pool_min_size,
            max_size=database.pool_max_size,
            connect_timeout_seconds=database.connect_timeout_seconds,
        )
        try:
            run_migrations(pool)
            completed = SQLiteToPostgresMigrator(
                source_path=arguments.source,
                destination=PostgresImportDestination(pool),
                destination_fingerprint=_destination_fingerprint(
                    database_url
                ),
            ).run(
                mode="apply",
                batch_size=arguments.batch_size,
                report_path=arguments.report,
                checkpoint_path=checkpoint_path,
            )
        finally:
            pool.close()
        _print_summary(completed)
        return 0
    except (MigrationError, ValueError) as error:
        code = (
            error.code
            if isinstance(error, MigrationError)
            else "INVALID_MIGRATION_ARGUMENT"
        )
        _print_error(code)
        return 2
    except Exception:
        _print_error("MIGRATION_UNAVAILABLE")
        return 2


def _print_summary(report: Any) -> None:
    print(
        json.dumps(
            {
                "mode": report.mode,
                "status": report.status,
                "completed_batches": report.completed_batches,
                "partial_envelope_rows": report.partial_envelope_rows,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _print_error(code: str) -> None:
    print(
        json.dumps(
            {"error_code": code},
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )


def _destination_fingerprint(database_url: str) -> str:
    """Bind a checkpoint to a target without hashing its password."""

    parsed = conninfo_to_dict(database_url)
    target_identity = {
        key: parsed[key]
        for key in ("host", "hostaddr", "port", "dbname", "user", "service")
        if parsed.get(key)
    }
    serialized = json.dumps(
        target_identity,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = ["build_parser", "main"]
