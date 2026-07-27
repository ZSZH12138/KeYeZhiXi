"""Regression coverage for the merged M4 and M6 forward migration ledger."""

from pathlib import Path

from course_insight.infrastructure.postgresql.migration_runner import (
    MIGRATIONS_DIRECTORY,
    SCHEMA_VERSION as POSTGRES_SCHEMA_VERSION,
    load_migrations,
)
from course_insight.infrastructure.sqlite import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION as SQLITE_SCHEMA_VERSION,
    migrate,
)


def test_m4_and_m6_use_one_contiguous_immutable_migration_sequence(
    tmp_path: Path,
) -> None:
    """Keep published M4 versions while appending M6 and its M0 freeze."""

    expected_tail = (
        "0010_m4_intent_decisions.sql",
        "0011_m4_intent_runtime_statuses.sql",
        "0012_m6_policy_learning.sql",
        "0013_m0_policy_freeze.sql",
    )

    assert SQLITE_SCHEMA_VERSION == 13
    assert POSTGRES_SCHEMA_VERSION == 13
    assert tuple(
        migration.path.name for migration in load_migrations()
    )[-4:] == expected_tail
    assert tuple(
        path.name
        for path in sorted(MIGRATIONS_DIRECTORY.glob("*.sql"))
    )[-4:] == expected_tail

    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        ledger = tuple(
            (int(row["version"]), str(row["name"]))
            for row in connection.execute(
                """
                SELECT version, name
                FROM schema_migrations
                WHERE version >= 10
                ORDER BY version
                """
            ).fetchall()
        )
        tables = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

    assert ledger == (
        (10, "m4_intent_decisions"),
        (11, "m4_intent_runtime_statuses"),
        (12, "m6_policy_learning"),
        (13, "m0_policy_freeze"),
    )
    assert {
        "m4_intent_decisions",
        "m6_policy_artifacts",
        "m6_policy_executions",
        "m6_policy_observations",
        "m6_policy_rewards",
        "m6_policy_evaluations",
    } <= tables
