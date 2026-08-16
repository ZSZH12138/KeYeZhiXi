"""Check production deployment prerequisites without mutating infrastructure."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys


def check_production_settings(settings: Mapping[str, object] | None) -> dict[str, object]:
    """Return a fail-closed readiness report from redacted deployment facts."""

    reasons: list[str] = []
    if settings is None:
        reasons.append("settings_missing")
    else:
        if settings.get("environment") != "production":
            reasons.append("environment_not_production")
        if settings.get("database_backend") != "postgresql":
            reasons.append("database_must_be_postgresql")
        if settings.get("pgvector_enabled") is not True:
            reasons.append("pgvector_not_enabled")
        if settings.get("secure_cookies") is not True:
            reasons.append("secure_cookies_required")
        hosts = settings.get("allowed_hosts")
        if not isinstance(hosts, list) or not hosts or any(
            not isinstance(host, str) or not host.strip() for host in hosts
        ):
            reasons.append("allowed_hosts_missing")
        if settings.get("migrations_applied") is not True:
            reasons.append("migrations_not_verified")
        if settings.get("m6_mode", "rules") != "rules":
            reasons.append("m6_must_remain_rules")
        rollout = settings.get("m6_rollout_percentage", 0.0)
        if type(rollout) not in {int, float} or float(rollout) != 0.0:
            reasons.append("m6_rollout_must_be_zero")
    return {
        "schema_version": "production-readiness/v1",
        "status": "passed" if not reasons else "blocked",
        "passed": not reasons,
        "checks": {
            "m7_m9_scope": "excluded",
            "m6_rollout": "zero",
        },
        "reasons": reasons,
    }


def _load(path: Path) -> Mapping[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = check_production_settings(_load(args.settings))
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.output is not None:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0 if report["passed"] is True else 2


if __name__ == "__main__":
    sys.exit(main())

