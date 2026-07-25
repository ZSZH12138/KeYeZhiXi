"""Real PostgreSQL checks for the core pool and migration layer."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pytest
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from course_insight.infrastructure.postgresql.migration_runner import (
    CORE_TABLES,
    SCHEMA_VERSION,
    current_schema_version,
    destroy_schema_for_tests,
    rebuild_schema_for_tests,
    run_migrations,
)
from course_insight.infrastructure.postgresql.pool import (
    PostgresPool,
    create_postgres_pool,
)
from tests.integration._postgres_live import require_live_test_database_url


@pytest.fixture(scope="module")
def postgres_pool() -> Iterator[PostgresPool]:
    database_url = require_live_test_database_url()
    pool = create_postgres_pool(
        database_url,
        min_size=1,
        max_size=2,
        connect_timeout_seconds=5,
    )
    try:
        yield pool
    finally:
        destroy_schema_for_tests(pool, allow_destructive=True)
        pool.close()


@pytest.fixture
def clean_schema(postgres_pool: PostgresPool) -> PostgresPool:
    rebuild_schema_for_tests(
        postgres_pool,
        allow_destructive=True,
    )
    return postgres_pool


def test_real_postgres_clean_initialization_repeat_and_rebuild(
    postgres_pool: PostgresPool,
) -> None:
    destroy_schema_for_tests(
        postgres_pool,
        allow_destructive=True,
    )
    assert current_schema_version(postgres_pool) == 0

    first = run_migrations(postgres_pool)
    repeated = run_migrations(postgres_pool)

    assert first.applied_versions == tuple(range(1, SCHEMA_VERSION + 1))
    assert first.current_version == SCHEMA_VERSION
    assert repeated.applied_versions == ()
    assert repeated.current_version == SCHEMA_VERSION
    assert current_schema_version(postgres_pool) == SCHEMA_VERSION
    with postgres_pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT tablename
            FROM pg_catalog.pg_tables
            WHERE schemaname = current_schema()
              AND tablename = ANY(%s)
            ORDER BY tablename
            """,
            (list(CORE_TABLES),),
        ).fetchall()
    assert [row["tablename"] for row in rows] == sorted(CORE_TABLES)


def test_real_postgres_pool_returns_dict_rows(
    clean_schema: PostgresPool,
) -> None:
    with clean_schema.connection() as connection:
        row = connection.execute(
            "SELECT 1 AS answer, TRUE AS healthy"
        ).fetchone()

    assert row == {"answer": 1, "healthy": True}


def test_real_postgres_uses_required_physical_types(
    clean_schema: PostgresPool,
) -> None:
    expected = {
        ("m0_learning_events", "occurred_at"): "timestamp with time zone",
        ("m0_learning_events", "payload"): "jsonb",
        ("m0_event_outbox", "record"): "text",
        ("m0_event_outbox", "version"): "integer",
        ("m4_task_plans", "payload_checksum"): "character",
        ("schema_migrations", "is_transactional"): "boolean",
    }
    with clean_schema.connection() as connection:
        rows = connection.execute(
            """
            SELECT table_name, column_name, data_type, character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND (table_name, column_name) IN (
                  ('m0_learning_events', 'occurred_at'),
                  ('m0_learning_events', 'payload'),
                  ('m0_event_outbox', 'record'),
                  ('m0_event_outbox', 'version'),
                  ('m4_task_plans', 'payload_checksum'),
                  ('schema_migrations', 'is_transactional')
              )
            """
        ).fetchall()

    actual = {
        (row["table_name"], row["column_name"]): row["data_type"]
        for row in rows
    }
    assert actual == expected
    checksum_row = next(
        row
        for row in rows
        if row["column_name"] == "payload_checksum"
    )
    assert checksum_row["character_maximum_length"] == 64


def test_real_postgres_enforces_outbox_lease_and_cascade_rules(
    clean_schema: PostgresPool,
) -> None:
    occurred_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    queued_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(CheckViolation):
        with clean_schema.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO m0_learning_events(
                        event_id,
                        idempotency_key,
                        event_type,
                        occurred_at,
                        payload
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        "event_invalid_lease",
                        "event_invalid_lease",
                        "assessment.completed",
                        occurred_at,
                        Jsonb({"attempt_id": "attempt_invalid"}),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO m0_event_outbox(
                        event_id,
                        record,
                        status,
                        attempt_count,
                        version,
                        available_at,
                        locked_by,
                        locked_at,
                        lease_until,
                        last_error_code,
                        created_at,
                        updated_at
                    ) VALUES (
                        %s, %s, 'processing', 0, 1, %s,
                        NULL, NULL, NULL, NULL, %s, %s
                    )
                    """,
                    (
                        "event_invalid_lease",
                        '{"event_id":"event_invalid_lease"}',
                        queued_at,
                        queued_at,
                        queued_at,
                    ),
                )

    with clean_schema.connection() as connection:
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO m0_learning_events(
                    event_id,
                    idempotency_key,
                    event_type,
                    occurred_at,
                    payload
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    "event_cascade",
                    "event_cascade",
                    "assessment.completed",
                    occurred_at,
                    Jsonb({"attempt_id": "attempt_cascade"}),
                ),
            )
            connection.execute(
                """
                INSERT INTO m0_event_outbox(
                    event_id,
                    record,
                    status,
                    attempt_count,
                    version,
                    available_at,
                    locked_by,
                    locked_at,
                    lease_until,
                    last_error_code,
                    created_at,
                    updated_at
                ) VALUES (
                    %s, %s, 'pending', 0, 1, %s,
                    NULL, NULL, NULL, NULL, %s, %s
                )
                """,
                (
                    "event_cascade",
                    '{"event_id":"event_cascade"}',
                    queued_at,
                    queued_at,
                    queued_at,
                ),
            )
            timestamps = connection.execute(
                """
                SELECT events.occurred_at, outbox.available_at
                FROM m0_learning_events AS events
                JOIN m0_event_outbox AS outbox USING (event_id)
                WHERE events.event_id = %s
                """,
                ("event_cascade",),
            ).fetchone()
            connection.execute(
                "DELETE FROM m0_learning_events WHERE event_id = %s",
                ("event_cascade",),
            )
            remaining = connection.execute(
                """
                SELECT COUNT(*) AS row_count
                FROM m0_event_outbox
                WHERE event_id = %s
                """,
                ("event_cascade",),
            ).fetchone()

    assert timestamps is not None
    assert timestamps["occurred_at"] == occurred_at
    assert timestamps["available_at"] == queued_at
    assert remaining == {"row_count": 0}


def test_real_postgres_enforces_payload_checksum_and_schema_version(
    clean_schema: PostgresPool,
) -> None:
    with pytest.raises(CheckViolation):
        with clean_schema.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO m4_task_plans(
                        task_id,
                        idempotency_key,
                        payload,
                        payload_checksum,
                        schema_version
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        "task_invalid_checksum",
                        "a" * 64,
                        Jsonb({"schema_version": "1.0"}),
                        "not-a-checksum",
                        "1.0",
                    ),
                )

    with clean_schema.connection() as connection:
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO m4_task_plans(
                    task_id,
                    idempotency_key,
                    payload,
                    payload_checksum,
                    schema_version
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    "task_valid_checksum",
                    "b" * 64,
                    Jsonb({"schema_version": "1.0", "task_id": "task_valid"}),
                    "c" * 64,
                    "1.0",
                ),
            )
            row = connection.execute(
                """
                SELECT payload, payload_checksum, schema_version
                FROM m4_task_plans
                WHERE task_id = %s
                """,
                ("task_valid_checksum",),
            ).fetchone()

    assert row == {
        "payload": {
            "schema_version": "1.0",
            "task_id": "task_valid",
        },
        "payload_checksum": "c" * 64,
        "schema_version": "1.0",
    }
