"""Export JSON Schemas for the public cross-module contracts."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import course_insight.contracts as contracts
from course_insight.contracts.base import ContractModel


def public_contract_types() -> dict[str, type[ContractModel]]:
    """Return every concrete contract exposed by the public registry."""

    registry: dict[str, type[ContractModel]] = {}
    for name in contracts.__all__:
        candidate = getattr(contracts, name)
        if (
            isinstance(candidate, type)
            and candidate is not ContractModel
            and issubclass(candidate, ContractModel)
        ):
            registry[name] = candidate
    return dict(sorted(registry.items()))


def _write_json_atomic(path: Path, payload: object) -> None:
    """Write one complete UTF-8 JSON document before replacing its target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
            newline="",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _remove_stale_schemas(output_dir: Path, expected_names: set[str]) -> None:
    """Remove schema files that are no longer part of the public registry."""

    for path in sorted(output_dir.glob("*.schema.json")):
        if path.name not in expected_names:
            path.unlink()


def export_schemas(*, output_dir: Path) -> Path:
    """Export the current public registry as deterministic JSON Schemas."""

    output_dir.mkdir(parents=True, exist_ok=True)
    registry = public_contract_types()
    expected_names = {f"{name}.schema.json" for name in registry}
    for name, model_type in registry.items():
        _write_json_atomic(
            output_dir / f"{name}.schema.json",
            model_type.model_json_schema(mode="validation"),
        )
    _remove_stale_schemas(output_dir, expected_names)
    return output_dir


def main() -> int:
    """Export repository schemas and report their dynamically derived count."""

    registry = public_contract_types()
    export_schemas(output_dir=PROJECT_ROOT / "contracts" / "schemas")
    print(f"exported {len(registry)} schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
