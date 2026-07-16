"""SQLite connection policy for the local modular monolith."""

from __future__ import annotations

import sqlite3
from pathlib import Path


_BUSY_TIMEOUT_MILLISECONDS = 5_000


def connect_sqlite(path: str | Path) -> sqlite3.Connection:
    """Open a row-based SQLite connection configured for explicit writes."""

    database = str(path)
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement is unavailable")
    except Exception:
        connection.close()
        raise
    return connection
