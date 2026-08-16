"""TDD checks for the PostgreSQL S1-S6 production migration."""

from __future__ import annotations

import os

import pytest

from tests.integration._postgres_live import require_live_test_database_url

from course_insight.infrastructure.postgresql.migration_runner import (
    CORE_TABLES,
    SCHEMA_VERSION,
    _DROP_TABLE_STATEMENTS,
    load_migrations,
)


def test_s1_s6_migration_is_the_next_checksum_locked_version() -> None:
    assert SCHEMA_VERSION == 15
    migrations = load_migrations()
    assert migrations[-1].version == 15
    migration = next(item for item in migrations if item.version == 14)
    assert migration.path.name == "0014_m1_m2_m3_capabilities.sql"
    assert migration.checksum
    sql = migration.sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    for table in (
        "m1_m2_m3_artifacts",
        "m2_vector_indexes",
        "m2_vector_documents",
        "m2_retrieval_audits",
        "m3_teacher_reviews",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert table in CORE_TABLES
        assert any(table in statement for statement in _DROP_TABLE_STATEMENTS)
    assert "embedding vector NOT NULL" in sql
    assert "vector_dims(embedding)" in sql
    assert "JSONB NOT NULL" in sql
    assert "CREATE INDEX" in sql
    metadata_migration = migrations[-1]
    assert metadata_migration.path.name == "0015_vector_index_metadata.sql"
    assert "ADD COLUMN metadata JSONB" in metadata_migration.sql


def test_s1_s6_migration_is_transaction_safe_and_parameterized_by_runner() -> None:
    migration = next(
        item for item in load_migrations() if item.version == SCHEMA_VERSION
    )
    assert all("%s" not in statement for statement in migration.statements)
    assert all(";" not in statement for statement in migration.statements)
    assert any("CHECK" in statement for statement in migration.statements)


@pytest.mark.skipif(
    not os.getenv("COURSE_INSIGHT_TEST_DATABASE_URL"),
    reason="live PostgreSQL/pgvector is not configured",
)
def test_live_postgres_s1_s6_migration_is_explicitly_opt_in() -> None:
    """The live check runs only against the confirmed disposable test DB."""

    from course_insight.infrastructure.postgresql.m1_m2_m3_repository import (
        PostgresM1M2M3Repository,
    )
    from course_insight.infrastructure.postgresql.pool import create_postgres_pool

    pool = create_postgres_pool(require_live_test_database_url())
    try:
        PostgresM1M2M3Repository(pool).initialize()
    finally:
        pool.close()
