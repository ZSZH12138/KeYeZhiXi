"""Checksum-locked PostgreSQL migrations independent from Django."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Protocol

import psycopg

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
    PostgresMigrationError,
)


SCHEMA_VERSION = 11
MIGRATION_LOCK_ID = 0x434F55525345494E
MIGRATIONS_DIRECTORY = Path(__file__).with_name("migrations")

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE CHECK (length(name) > 0),
    checksum CHAR(64) NOT NULL
        CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_transactional BOOLEAN NOT NULL DEFAULT TRUE CHECK (is_transactional)
)
"""

CORE_TABLES = (
    "m0_event_outbox",
    "m0_assessment_runs",
    "m0_learning_events",
    "m4_task_plans",
    "m4_intent_decisions",
    "m5_state_updates",
    "m5_class_states",
    "m5_learner_states",
    "m6_tutoring_decisions",
    "m6_session_states",
    "m7_student_feedback",
    "m8_scoring_results",
    "m8_score_audits",
    "m8_assessment_papers",
    "m9_teacher_reviews",
    "m9_teacher_analytics",
)

_DROP_TABLE_STATEMENTS = (
    "DROP TABLE IF EXISTS m0_event_outbox",
    "DROP TABLE IF EXISTS m0_assessment_runs",
    "DROP TABLE IF EXISTS m0_learning_events",
    "DROP TABLE IF EXISTS m4_task_plans",
    "DROP TABLE IF EXISTS m4_intent_decisions",
    "DROP TABLE IF EXISTS m5_state_updates",
    "DROP TABLE IF EXISTS m5_class_states",
    "DROP TABLE IF EXISTS m5_learner_states",
    "DROP TABLE IF EXISTS m6_tutoring_decisions",
    "DROP TABLE IF EXISTS m6_session_states",
    "DROP TABLE IF EXISTS m7_student_feedback",
    "DROP TABLE IF EXISTS m8_scoring_results",
    "DROP TABLE IF EXISTS m8_score_audits",
    "DROP TABLE IF EXISTS m8_assessment_papers",
    "DROP TABLE IF EXISTS m9_teacher_reviews",
    "DROP TABLE IF EXISTS m9_teacher_analytics",
    "DROP TABLE IF EXISTS schema_migrations",
)

_MIGRATION_FILENAME = re.compile(
    r"^(?P<version>[0-9]{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$"
)
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"
_SELECT_APPLIED_SQL = """
SELECT version, name, checksum
FROM schema_migrations
ORDER BY version
"""
_INSERT_APPLIED_SQL = """
INSERT INTO schema_migrations(
    version,
    name,
    checksum,
    applied_at,
    is_transactional
) VALUES (%s, %s, %s, CURRENT_TIMESTAMP, TRUE)
"""


class _Cursor(Protocol):
    def fetchall(self) -> list[dict[str, object]]:
        """Return every selected row."""

    def fetchone(self) -> dict[str, object] | None:
        """Return one selected row."""


class _Connection(Protocol):
    def transaction(self) -> AbstractContextManager[object]:
        """Open an explicit database transaction."""

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...] | None = None,
    ) -> _Cursor:
        """Execute one parameterized statement."""


class MigrationPool(Protocol):
    def connection(self) -> AbstractContextManager[_Connection]:
        """Yield one dict-row connection."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable numbered SQL migration."""

    version: int
    name: str
    checksum: str
    path: Path
    sql: str
    statements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Safe migration outcome without connection or filesystem details."""

    applied_versions: tuple[int, ...]
    current_version: int


@dataclass(frozen=True, slots=True)
class _AppliedMigration:
    version: int
    name: str
    checksum: str


def split_sql_statements(sql_text: str) -> tuple[str, ...]:
    """Split SQL only at top-level semicolons."""

    if type(sql_text) is not str:
        raise TypeError("SQL migration source must be text")
    statements: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment_depth = 0

    while index < len(sql_text):
        character = sql_text[index]
        next_character = (
            sql_text[index + 1] if index + 1 < len(sql_text) else ""
        )
        if line_comment:
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment_depth:
            if character == "/" and next_character == "*":
                block_comment_depth += 1
                index += 2
                continue
            if character == "*" and next_character == "/":
                block_comment_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if dollar_tag is not None:
            if sql_text.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote is not None:
            if character == quote:
                if next_character == quote:
                    index += 2
                    continue
                quote = None
                index += 1
                continue
            if character == "\\" and index + 1 < len(sql_text):
                index += 2
                continue
            index += 1
            continue
        if character == "-" and next_character == "-":
            line_comment = True
            index += 2
            continue
        if character == "/" and next_character == "*":
            block_comment_depth = 1
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql_text[index:])
            if match is not None:
                dollar_tag = match.group(0)
                index += len(dollar_tag)
                continue
        if character == ";":
            _append_statement(statements, sql_text[start:index])
            start = index + 1
        index += 1

    if quote is not None:
        raise PostgresMigrationError("migration contains an unterminated quote")
    if dollar_tag is not None:
        raise PostgresMigrationError(
            "migration contains an unterminated dollar quote"
        )
    if block_comment_depth:
        raise PostgresMigrationError(
            "migration contains an unterminated block comment"
        )
    _append_statement(statements, sql_text[start:])
    return tuple(statements)


def load_migrations(
    directory: Path | None = None,
) -> tuple[Migration, ...]:
    """Load one contiguous immutable migration sequence."""

    selected_directory = (
        MIGRATIONS_DIRECTORY if directory is None else Path(directory)
    )
    if not selected_directory.is_dir():
        raise PostgresMigrationError("PostgreSQL migration directory is missing")
    candidates = sorted(selected_directory.glob("*.sql"), key=lambda path: path.name)
    if not candidates:
        raise PostgresMigrationError("PostgreSQL migration set is empty")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for path in candidates:
        match = _MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise PostgresMigrationError(
                f"invalid migration filename: {path.name}"
            )
        version = int(match.group("version"))
        if version < 1:
            raise PostgresMigrationError("migration versions must be positive")
        if version in seen_versions:
            raise PostgresMigrationError(
                f"duplicate migration version {version}"
            )
        seen_versions.add(version)
        try:
            payload = path.read_bytes()
            sql_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise PostgresMigrationError(
                f"migration {version} is not valid UTF-8"
            ) from None
        except OSError:
            raise PostgresMigrationError(
                f"migration {version} cannot be read"
            ) from None
        statements = split_sql_statements(sql_text)
        if not statements:
            raise PostgresMigrationError(
                f"migration {version} contains no SQL statements"
            )
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                checksum=hashlib.sha256(payload).hexdigest(),
                path=path,
                sql=sql_text,
                statements=statements,
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    actual_versions = tuple(migration.version for migration in migrations)
    expected_versions = tuple(range(1, len(migrations) + 1))
    if actual_versions != expected_versions:
        raise PostgresMigrationError(
            "migration files must be contiguous starting at version 1"
        )
    if (
        directory is None
        and migrations[-1].version != SCHEMA_VERSION
    ):
        raise PostgresMigrationError(
            "bundled migration version does not match the application"
        )
    return tuple(migrations)


def run_migrations(
    pool: MigrationPool,
    *,
    directory: Path | None = None,
) -> MigrationReport:
    """Apply pending migrations under one transaction-scoped advisory lock."""

    migrations = load_migrations(directory)
    try:
        with pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    _ADVISORY_LOCK_SQL,
                    (MIGRATION_LOCK_ID,),
                )
                connection.execute(MIGRATION_TABLE_SQL)
                applied = _read_applied(connection)
                _validate_applied(applied, migrations)
                already_applied = {migration.version for migration in applied}
                newly_applied: list[int] = []
                for migration in migrations:
                    if migration.version in already_applied:
                        continue
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        _INSERT_APPLIED_SQL,
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                        ),
                    )
                    newly_applied.append(migration.version)
    except (PostgresConnectionError, PostgresMigrationError):
        raise
    except psycopg.Error:
        raise PostgresMigrationError(
            "PostgreSQL migration execution failed"
        ) from None
    return MigrationReport(
        applied_versions=tuple(newly_applied),
        current_version=migrations[-1].version,
    )


def current_schema_version(
    pool: MigrationPool,
    *,
    directory: Path | None = None,
) -> int:
    """Return zero for a clean database or one fully verified version."""

    migrations = load_migrations(directory)
    try:
        with pool.connection() as connection:
            relation = connection.execute(
                "SELECT to_regclass(%s) AS relation_name",
                ("schema_migrations",),
            ).fetchone()
            if relation is None or relation["relation_name"] is None:
                return 0
            applied = _read_applied(connection)
            _validate_applied(applied, migrations)
    except PostgresConnectionError:
        raise
    except psycopg.Error:
        raise PostgresMigrationError(
            "PostgreSQL schema version query failed"
        ) from None
    return 0 if not applied else applied[-1].version


def schema_is_current(
    pool: MigrationPool,
    *,
    directory: Path | None = None,
) -> bool:
    """Return false for a clean, incomplete, drifted, or future schema."""

    migrations = load_migrations(directory)
    try:
        with pool.connection() as connection:
            relation = connection.execute(
                "SELECT to_regclass(%s) AS relation_name",
                ("schema_migrations",),
            ).fetchone()
            if relation is None or relation["relation_name"] is None:
                return False
            applied = _read_applied(connection)
    except PostgresConnectionError:
        raise
    except psycopg.Error:
        raise PostgresMigrationError(
            "PostgreSQL schema state query failed"
        ) from None
    try:
        _validate_applied(applied, migrations)
    except PostgresMigrationError:
        return False
    return len(applied) == len(migrations)


def destroy_schema_for_tests(
    pool: MigrationPool,
    *,
    allow_destructive: bool = False,
) -> None:
    """Drop only known core tables after an explicit test-only opt-in."""

    if allow_destructive is not True:
        raise ValueError("test schema destruction requires explicit destructive opt-in")
    try:
        with pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    _ADVISORY_LOCK_SQL,
                    (MIGRATION_LOCK_ID,),
                )
                for statement in _DROP_TABLE_STATEMENTS:
                    connection.execute(statement)
    except (PostgresConnectionError, PostgresMigrationError):
        raise
    except psycopg.Error:
        raise PostgresMigrationError(
            "PostgreSQL test schema destruction failed"
        ) from None


def rebuild_schema_for_tests(
    pool: MigrationPool,
    *,
    allow_destructive: bool = False,
    directory: Path | None = None,
) -> MigrationReport:
    """Destroy and recreate the known core schema for integration tests."""

    destroy_schema_for_tests(
        pool,
        allow_destructive=allow_destructive,
    )
    return run_migrations(pool, directory=directory)


def _read_applied(connection: _Connection) -> tuple[_AppliedMigration, ...]:
    rows = connection.execute(_SELECT_APPLIED_SQL).fetchall()
    applied: list[_AppliedMigration] = []
    try:
        for row in rows:
            version = row["version"]
            name = row["name"]
            checksum = row["checksum"]
            if (
                type(version) is not int
                or type(name) is not str
                or type(checksum) is not str
            ):
                raise ValueError
            applied.append(
                _AppliedMigration(
                    version=version,
                    name=name,
                    checksum=checksum,
                )
            )
    except (KeyError, TypeError, ValueError):
        raise PostgresMigrationError("migration metadata is invalid") from None
    return tuple(applied)


def _validate_applied(
    applied: Sequence[_AppliedMigration],
    migrations: Sequence[Migration],
) -> None:
    supported = {migration.version: migration for migration in migrations}
    versions = tuple(migration.version for migration in applied)
    if len(versions) != len(set(versions)):
        raise PostgresMigrationError("migration metadata has duplicate versions")
    if any(version < 1 for version in versions):
        raise PostgresMigrationError("migration metadata is invalid")
    latest_supported = migrations[-1].version
    if versions and max(versions) > latest_supported:
        raise PostgresMigrationError(
            "database schema is newer than this application"
        )
    if versions:
        expected = set(range(1, max(versions) + 1))
        missing = sorted(expected.difference(versions))
        if missing:
            raise PostgresMigrationError(
                f"missing applied migration version {missing[0]}"
            )
    for recorded in applied:
        expected_migration = supported[recorded.version]
        if recorded.name != expected_migration.name:
            raise PostgresMigrationError(
                f"migration name drift at version {recorded.version}"
            )
        if (
            _LOWER_SHA256.fullmatch(recorded.checksum) is None
            or recorded.checksum != expected_migration.checksum
        ):
            raise PostgresMigrationError(
                f"migration checksum drift at version {recorded.version}"
            )


def _append_statement(statements: list[str], candidate: str) -> None:
    statement = candidate.strip()
    if statement and _has_executable_content(statement):
        statements.append(statement)


def _has_executable_content(statement: str) -> bool:
    index = 0
    block_depth = 0
    line_comment = False
    while index < len(statement):
        character = statement[index]
        next_character = (
            statement[index + 1] if index + 1 < len(statement) else ""
        )
        if line_comment:
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_depth:
            if character == "/" and next_character == "*":
                block_depth += 1
                index += 2
                continue
            if character == "*" and next_character == "/":
                block_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if character == "-" and next_character == "-":
            line_comment = True
            index += 2
            continue
        if character == "/" and next_character == "*":
            block_depth = 1
            index += 2
            continue
        if not character.isspace():
            return True
        index += 1
    return False


__all__ = [
    "CORE_TABLES",
    "MIGRATION_LOCK_ID",
    "MIGRATION_TABLE_SQL",
    "MIGRATIONS_DIRECTORY",
    "Migration",
    "MigrationPool",
    "MigrationReport",
    "SCHEMA_VERSION",
    "current_schema_version",
    "destroy_schema_for_tests",
    "load_migrations",
    "rebuild_schema_for_tests",
    "run_migrations",
    "schema_is_current",
    "split_sql_statements",
]
