"""Fresh-process and migration-boundary tests for the SQLite S1-S6 slice."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from course_insight.infrastructure.sqlite.m1_m2_m3_repository import (
    SQLiteM1M2M3Repository,
)
from course_insight.infrastructure.sqlite.migrations_s1_s6 import (
    S1_S6_SCHEMA_VERSION,
    current_schema_version,
)
from tests.unit.test_s1_s6_persistence import (
    _bundle_artifact,
    _course_import,
    _index_artifact,
)


def test_s1_s6_schema_is_versioned_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = SQLiteM1M2M3Repository(database)
    repository.initialize()
    repository.initialize()

    assert current_schema_version(database) == S1_S6_SCHEMA_VERSION


def test_sqlite_repository_restores_from_a_fresh_process(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = SQLiteM1M2M3Repository(database)
    repository.initialize()
    package, snapshot = _course_import()
    index, lexical_snapshot = _index_artifact(package)
    bundle, report, seed = _bundle_artifact(tmp_path, package)
    repository.save_course_import(package, snapshot)
    repository.save_index_artifact(index, lexical_snapshot)
    repository.save_bundle_artifact(bundle, report, seed)

    program = (
        "from pathlib import Path\n"
        "from course_insight.infrastructure.sqlite.m1_m2_m3_repository "
        "import SQLiteM1M2M3Repository\n"
        f"repo = SQLiteM1M2M3Repository(Path({str(database)!r}))\n"
        "repo.initialize()\n"
        "package = repo.get_course_package('package_1', 'v1')\n"
        "index = repo.get_index('index_1', 'v1')\n"
        "bundle = repo.get_knowledge_bundle('bundle_1', 'v1')\n"
        "assert package is not None and index is not None and bundle is not None\n"
        "print(package.checksum + ':' + index.checksum + ':' + bundle.content_checksum())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ":".join(
        (package.checksum, index.checksum, bundle.content_checksum())
    )
