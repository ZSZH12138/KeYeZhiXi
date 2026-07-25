"""Portable wrapper for the explicit SQLite to PostgreSQL migration."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from course_insight.infrastructure.postgresql.sqlite_import_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
