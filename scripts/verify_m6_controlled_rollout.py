"""Verify M6 offline evidence without enabling a production rollout.

This command is intentionally fail-closed.  It checks only M6 policy evidence;
it does not train, publish, call an LLM, or connect M6 to M7/M9.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys


def verify_evidence(evidence: Mapping[str, object] | None) -> dict[str, object]:
    """Return a redacted, deterministic controlled-verification report."""

    base: dict[str, object] = {
        "schema_version": "m6-controlled-verification/v1",
        "rollout_percentage": 0.0,
        "mode": "rules",
        "passed": False,
        "status": "blocked",
        "reasons": [],
    }
    if evidence is None:
        base["reasons"] = ["evidence_missing"]
        return base
    reasons: list[str] = []
    required_text = ("policy_id", "dataset_identity")
    for field in required_text:
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            reasons.append(f"{field}_missing")
    if evidence.get("ope_status") != "sufficient_data":
        reasons.append("ope_not_sufficient")
    if evidence.get("approved") is not True:
        reasons.append("approval_missing")
    if evidence.get("mode", "rules") != "rules":
        reasons.append("mode_must_remain_rules")
    rollout = evidence.get("rollout_percentage", 0.0)
    if type(rollout) not in {int, float} or float(rollout) != 0.0:
        reasons.append("rollout_must_be_zero")
    if evidence.get("global_kill_switch", False) is not False:
        reasons.append("kill_switch_state_invalid")
    base["reasons"] = reasons
    base["status"] = "passed" if not reasons else "blocked"
    base["passed"] = not reasons
    return base


def _load(path: Path) -> Mapping[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = verify_evidence(_load(args.evidence))
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if args.output is not None:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0 if report["passed"] is True else 2


if __name__ == "__main__":
    sys.exit(main())

