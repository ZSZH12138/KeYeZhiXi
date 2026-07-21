"""Public SQLite infrastructure boundary."""

from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLiteM0Repository",
    "connect_sqlite",
    "current_schema_version",
    "migrate",
]
