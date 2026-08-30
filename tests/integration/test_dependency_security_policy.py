from __future__ import annotations

import tomllib
from pathlib import Path


def test_runtime_dependencies_exclude_audited_vulnerable_versions() -> None:
    project_root = Path(__file__).resolve().parents[2]
    project = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = set(project["project"]["dependencies"])
    constraints = (
        project_root / "requirements" / "ci-constraints.txt"
    ).read_text(encoding="utf-8")

    assert "Django>=5.2.17,<5.3" in dependencies
    assert "sqlparse>=0.6,<0.7" in dependencies
    assert "Django==5.2.17" in constraints
    assert "sqlparse==0.6.0" in constraints
