from __future__ import annotations

from pathlib import Path
import tomllib


def _pytest_options() -> dict[str, object]:
    project_root = Path(__file__).resolve().parents[2]
    with (project_root / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    return document["tool"]["pytest"]["ini_options"]


def test_pytest_only_discovers_current_test_tree() -> None:
    options = _pytest_options()

    assert options["testpaths"] == ["tests"]
    assert "runtime" in options["norecursedirs"]
