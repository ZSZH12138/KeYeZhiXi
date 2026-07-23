"""Public SQLite infrastructure boundary."""

from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.infrastructure.sqlite.m6_repository import SQLiteM6Repository
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
)

__all__ = [
    "SCHEMA_VERSION",
    "SQLiteM0Repository",
    "SQLiteM4Repository",
    "SQLiteM6Repository",
    "connect_sqlite",
    "current_schema_version",
    "migrate",
]
