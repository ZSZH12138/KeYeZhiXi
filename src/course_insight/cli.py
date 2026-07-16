"""Command-line entry point for the local project skeleton."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform import M0PlatformService


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_init(runtime_dir: Path) -> int:
    """Initialize local platform storage and print its component status."""

    service = M0PlatformService(
        database_path=runtime_dir / "course_insight.sqlite3",
        runtime_dir=runtime_dir,
        config_dir=PROJECT_ROOT / "config",
    )
    service.initialize()
    print(json.dumps(service.health_check(), sort_keys=True))
    return 0


def _run_export_schemas() -> int:
    """Delegate public-contract export to the repository utility."""

    from scripts.export_schemas import main as export_main

    return export_main()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="course-insight")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_command = subcommands.add_parser("init")
    init_command.add_argument(
        "--runtime-dir",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "local",
    )
    subcommands.add_parser("export-schemas")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a local command with safe domain-error reporting."""

    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            return _run_init(args.runtime_dir)
        return _run_export_schemas()
    except DomainError as error:
        print(f"{error.module}:{error.code}: {error.message}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"command failed safely: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
