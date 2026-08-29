"""Unit tests for the PostgreSQL connection and pool boundary."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from math import inf, nan
from pathlib import Path
from typing import Iterator

import pytest
from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout

from course_insight.infrastructure.postgresql import connection as connection_module
from course_insight.infrastructure.postgresql import pool as pool_module
from course_insight.infrastructure.postgresql.connection import (
    PostgresConnectionError,
    connect_postgres,
)
from course_insight.infrastructure.postgresql.pool import (
    PostgresPool,
    create_postgres_pool,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresMigrationError,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    CORE_TABLES,
    MIGRATION_LOCK_ID,
    MIGRATION_TABLE_SQL,
    SCHEMA_VERSION,
    current_schema_version,
    destroy_schema_for_tests,
    load_migrations,
    rebuild_schema_for_tests,
    run_migrations,
    schema_is_current,
    split_sql_statements,
)


_SECRET_DSN = "postgresql://private_user:private_password@db.example/app"


class _FakeDriverPool:
    def __init__(self) -> None:
        self.open_calls: list[tuple[bool, float]] = []
        self.connection_timeouts: list[float | None] = []
        self.close_calls = 0
        self.open_error: Exception | None = None
        self.connection_error: Exception | None = None
        self.connection_value: object = {"row": "dict"}

    def open(self, *, wait: bool, timeout: float) -> None:
        self.open_calls.append((wait, timeout))
        if self.open_error is not None:
            raise self.open_error

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[object]:
        self.connection_timeouts.append(timeout)
        if self.connection_error is not None:
            raise self.connection_error
        yield self.connection_value

    def close(self) -> None:
        self.close_calls += 1


def test_connect_postgres_uses_dict_rows_and_ceil_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[tuple[str, object, int]] = []

    def fake_connect(
        dsn: str,
        *,
        row_factory: object,
        connect_timeout: int,
    ) -> object:
        calls.append((dsn, row_factory, connect_timeout))
        return sentinel

    monkeypatch.setattr(connection_module.psycopg, "connect", fake_connect)

    connection = connect_postgres(
        _SECRET_DSN,
        connect_timeout_seconds=2.2,
    )

    assert connection is sentinel
    assert calls == [(_SECRET_DSN, dict_row, 3)]


@pytest.mark.parametrize(
    "timeout",
    [0, -1, nan, inf, True],
)
def test_connect_postgres_rejects_invalid_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="connect timeout"):
        connect_postgres(_SECRET_DSN, connect_timeout_seconds=timeout)  # type: ignore[arg-type]


def test_connect_postgres_redacts_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_connect(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OperationalError(f"could not connect to {_SECRET_DSN}")

    monkeypatch.setattr(connection_module.psycopg, "connect", fail_connect)

    with pytest.raises(PostgresConnectionError) as captured:
        connect_postgres(_SECRET_DSN, connect_timeout_seconds=1)

    assert _SECRET_DSN not in str(captured.value)
    assert "private_password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_create_pool_configures_dict_rows_and_waits_until_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_pool = _FakeDriverPool()
    constructor_calls: list[dict[str, object]] = []
    expected_check = pool_module.ConnectionPool.check_connection

    def fake_pool_constructor(**kwargs: object) -> _FakeDriverPool:
        constructor_calls.append(kwargs)
        return driver_pool

    monkeypatch.setattr(pool_module, "ConnectionPool", fake_pool_constructor)

    pool = create_postgres_pool(
        _SECRET_DSN,
        min_size=2,
        max_size=5,
        connect_timeout_seconds=2.2,
    )

    assert isinstance(pool, PostgresPool)
    assert driver_pool.open_calls == [(True, 2.2)]
    assert constructor_calls == [
        {
            "conninfo": _SECRET_DSN,
            "min_size": 2,
            "max_size": 5,
            "kwargs": {
                "connect_timeout": 3,
                "row_factory": dict_row,
            },
            "open": False,
            "timeout": 2.2,
            "check": expected_check,
        }
    ]
    assert _SECRET_DSN not in repr(pool)
    assert "private_password" not in repr(pool)


def test_pool_connection_yields_driver_connection_and_forwards_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_pool = _FakeDriverPool()

    def fake_pool_constructor(**kwargs: object) -> _FakeDriverPool:
        del kwargs
        return driver_pool

    monkeypatch.setattr(pool_module, "ConnectionPool", fake_pool_constructor)
    pool = create_postgres_pool(
        _SECRET_DSN,
        min_size=1,
        max_size=1,
        connect_timeout_seconds=1,
        open=False,
    )
    pool.open()

    with pool.connection(timeout=0.75) as connection:
        assert connection is driver_pool.connection_value

    assert driver_pool.open_calls == [(True, 1.0)]
    assert driver_pool.connection_timeouts == [0.75]


@pytest.mark.parametrize(
    "driver_error",
    [
        OperationalError(f"connection failed for {_SECRET_DSN}"),
        PoolTimeout(f"pool timed out for {_SECRET_DSN}"),
    ],
)
def test_pool_redacts_connection_acquisition_errors(
    monkeypatch: pytest.MonkeyPatch,
    driver_error: Exception,
) -> None:
    driver_pool = _FakeDriverPool()
    driver_pool.connection_error = driver_error

    def fake_pool_constructor(**kwargs: object) -> _FakeDriverPool:
        del kwargs
        return driver_pool

    monkeypatch.setattr(pool_module, "ConnectionPool", fake_pool_constructor)
    pool = create_postgres_pool(
        _SECRET_DSN,
        min_size=1,
        max_size=1,
        connect_timeout_seconds=1,
        open=False,
    )

    with pytest.raises(PostgresConnectionError) as captured:
        with pool.connection():
            pytest.fail("connection acquisition unexpectedly succeeded")

    assert _SECRET_DSN not in str(captured.value)
    assert "private_password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_pool_redacts_operational_error_raised_while_connection_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_pool = _FakeDriverPool()

    def fake_pool_constructor(**kwargs: object) -> _FakeDriverPool:
        del kwargs
        return driver_pool

    monkeypatch.setattr(pool_module, "ConnectionPool", fake_pool_constructor)
    pool = create_postgres_pool(
        _SECRET_DSN,
        min_size=1,
        max_size=1,
        connect_timeout_seconds=1,
        open=False,
    )

    with pytest.raises(PostgresConnectionError) as captured:
        with pool.connection():
            raise OperationalError(f"connection lost for {_SECRET_DSN}")

    assert _SECRET_DSN not in str(captured.value)
    assert "private_password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_pool_does_not_mask_non_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_pool = _FakeDriverPool()

    def fake_pool_constructor(**kwargs: object) -> _FakeDriverPool:
        del kwargs
        return driver_pool

    monkeypatch.setattr(pool_module, "ConnectionPool", fake_pool_constructor)
    pool = create_postgres_pool(
        _SECRET_DSN,
        min_size=1,
        max_size=1,
        connect_timeout_seconds=1,
        open=False,
    )

    with pytest.raises(ValueError, match="repository validation failed"):
        with pool.connection():
            raise ValueError("repository validation failed")


def test_pool_open_error_is_redacted_and_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver_pool = _FakeDriverPool()
    driver_pool.open_error = OperationalError(
        f"could not open {_SECRET_DSN}"
    )

    def fake_pool_constructor(**kwargs: object) -> _FakeDriverPool:
        del kwargs
        return driver_pool

    monkeypatch.setattr(pool_module, "ConnectionPool", fake_pool_constructor)
    pool = create_postgres_pool(
        _SECRET_DSN,
        min_size=1,
        max_size=1,
        connect_timeout_seconds=1,
        open=False,
    )

    with pytest.raises(PostgresConnectionError) as captured:
        pool.open()

    assert _SECRET_DSN not in str(captured.value)
    assert "private_password" not in str(captured.value)
    pool.close()
    pool.close()
    assert driver_pool.close_calls == 1


@pytest.mark.parametrize(
    ("min_size", "max_size"),
    [(0, 1), (2, 1), (1, 0), (True, 1)],
)
def test_create_pool_rejects_invalid_sizes(
    min_size: object,
    max_size: object,
) -> None:
    with pytest.raises(ValueError, match="pool size"):
        create_postgres_pool(
            _SECRET_DSN,
            min_size=min_size,  # type: ignore[arg-type]
            max_size=max_size,  # type: ignore[arg-type]
            connect_timeout_seconds=1,
            open=False,
        )


class _FakeCursor:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows = [] if rows is None else rows

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]

    def fetchone(self) -> dict[str, object] | None:
        return None if not self._rows else dict(self._rows[0])


class _FakeMigrationConnection:
    def __init__(
        self,
        *,
        applied: dict[int, tuple[str, str]] | None = None,
        migration_table_exists: bool | None = None,
    ) -> None:
        self.applied = {} if applied is None else dict(applied)
        self.migration_table_exists = (
            bool(self.applied)
            if migration_table_exists is None
            else migration_table_exists
        )
        self.executions: list[
            tuple[str, tuple[object, ...] | None, bool]
        ] = []
        self.commits = 0
        self.rollbacks = 0
        self._in_transaction = False
        self.fail_on_statement: str | None = None
        self.failure_text = "sensitive driver failure"

    @contextmanager
    def transaction(self) -> Iterator[None]:
        applied_before = dict(self.applied)
        table_before = self.migration_table_exists
        assert not self._in_transaction
        self._in_transaction = True
        try:
            yield
        except Exception:
            self.applied = applied_before
            self.migration_table_exists = table_before
            self.rollbacks += 1
            raise
        else:
            self.commits += 1
        finally:
            self._in_transaction = False

    def execute(
        self,
        query: str,
        parameters: tuple[object, ...] | None = None,
    ) -> _FakeCursor:
        normalized = " ".join(query.split())
        self.executions.append(
            (normalized, parameters, self._in_transaction)
        )
        if (
            self.fail_on_statement is not None
            and self.fail_on_statement in query
        ):
            raise OperationalError(self.failure_text)
        if normalized.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            self.migration_table_exists = True
            return _FakeCursor()
        if normalized.startswith("SELECT version, name, checksum"):
            return _FakeCursor(
                [
                    {
                        "version": version,
                        "name": name,
                        "checksum": checksum,
                    }
                    for version, (name, checksum) in sorted(
                        self.applied.items()
                    )
                ]
            )
        if normalized.startswith("INSERT INTO schema_migrations"):
            assert parameters is not None
            version, name, checksum = parameters
            self.applied[int(version)] = (str(name), str(checksum))
            return _FakeCursor()
        if normalized.startswith("SELECT to_regclass"):
            relation_name = (
                "schema_migrations"
                if self.migration_table_exists
                else None
            )
            return _FakeCursor([{"relation_name": relation_name}])
        if normalized.startswith("SELECT COALESCE(MAX(version), 0)"):
            version = max(self.applied, default=0)
            return _FakeCursor([{"version": version}])
        if normalized.startswith("DROP TABLE IF EXISTS schema_migrations"):
            self.applied = {}
            self.migration_table_exists = False
            return _FakeCursor()
        return _FakeCursor()


class _FakeMigrationPool:
    def __init__(self, connection: _FakeMigrationConnection) -> None:
        self.connection_value = connection
        self.connection_calls = 0

    @contextmanager
    def connection(self) -> Iterator[_FakeMigrationConnection]:
        self.connection_calls += 1
        yield self.connection_value


def _write_migration(
    directory: Path,
    version: int,
    name: str,
    sql: str,
) -> Path:
    path = directory / f"{version:04d}_{name}.sql"
    path.write_text(sql, encoding="utf-8", newline="\n")
    return path


def test_split_sql_statements_preserves_quoted_and_dollar_semicolons() -> None:
    sql = """
    -- a line comment containing ;
    CREATE TABLE example (
        value TEXT DEFAULT 'semi;colon',
        "odd;identifier" TEXT
    );
    /* a block comment containing ; */
    INSERT INTO example(value) VALUES ('it''s;canonical');
    DO $migration_body$
    BEGIN
        RAISE NOTICE 'still;one;statement';
    END
    $migration_body$;
    """

    statements = split_sql_statements(sql)

    assert len(statements) == 3
    assert statements[0].lstrip().startswith("-- a line comment")
    assert statements[1].lstrip().startswith("/* a block comment")
    assert statements[2].lstrip().startswith("DO $migration_body$")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'unterminated;",
        'SELECT "unterminated;',
        "/* unterminated",
        "DO $body$ BEGIN NULL; END;",
    ],
)
def test_split_sql_statements_rejects_unterminated_lexemes(sql: str) -> None:
    with pytest.raises(PostgresMigrationError, match="unterminated"):
        split_sql_statements(sql)


def test_load_migrations_uses_raw_sha256_and_fixed_contiguous_numbers(
    tmp_path: Path,
) -> None:
    first = _write_migration(
        tmp_path,
        1,
        "first",
        "CREATE TABLE first_table(id INTEGER);\n",
    )
    second = _write_migration(
        tmp_path,
        2,
        "second",
        "CREATE TABLE second_table(id INTEGER);\n",
    )

    migrations = load_migrations(tmp_path)

    assert [migration.version for migration in migrations] == [1, 2]
    assert [migration.name for migration in migrations] == [
        "first",
        "second",
    ]
    assert migrations[0].checksum == hashlib.sha256(
        first.read_bytes()
    ).hexdigest()
    assert migrations[1].checksum == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


def test_load_migrations_rejects_gaps_duplicate_versions_and_bad_names(
    tmp_path: Path,
) -> None:
    _write_migration(tmp_path, 1, "first", "SELECT 1;")
    _write_migration(tmp_path, 3, "third", "SELECT 3;")
    with pytest.raises(PostgresMigrationError, match="contiguous"):
        load_migrations(tmp_path)

    (tmp_path / "0003_third.sql").unlink()
    _write_migration(tmp_path, 1, "duplicate", "SELECT 2;")
    with pytest.raises(PostgresMigrationError, match="duplicate"):
        load_migrations(tmp_path)

    (tmp_path / "0001_duplicate.sql").unlink()
    (tmp_path / "bad-name.sql").write_text("SELECT 2;", encoding="utf-8")
    with pytest.raises(PostgresMigrationError, match="filename"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_empty_or_invalid_utf8_without_path_leak(
    tmp_path: Path,
) -> None:
    path = tmp_path / "0001_empty.sql"
    path.write_text("-- no executable statement\n", encoding="utf-8")
    with pytest.raises(PostgresMigrationError, match="no SQL statements") as empty:
        load_migrations(tmp_path)
    assert str(tmp_path) not in str(empty.value)

    path.write_bytes(b"\xff\xfe")
    with pytest.raises(PostgresMigrationError, match="UTF-8") as invalid:
        load_migrations(tmp_path)
    assert str(tmp_path) not in str(invalid.value)


def test_default_migrations_cover_exact_current_backend_tables_and_types() -> None:
    migrations = load_migrations()

    assert tuple(migration.version for migration in migrations) == tuple(
        range(1, SCHEMA_VERSION + 1)
    )
    assert tuple(migration.path.name for migration in migrations) == (
        "0001_m0_platform.sql",
        "0002_m4_task_orchestration.sql",
        "0003_m5_learner_class_state.sql",
        "0004_m6_tutoring_fsm.sql",
        "0005_m7_local_model.sql",
        "0006_m8_assessment_scoring.sql",
        "0007_m9_teacher_analytics.sql",
        "0008_m0_review_workflow.sql",
        "0009_m0_workflow_recovery_freeze.sql",
        "0010_m4_intent_decisions.sql",
        "0011_m4_intent_runtime_statuses.sql",
        "0012_m6_policy_learning.sql",
        "0013_m0_policy_freeze.sql",
        "0014_m5_m8_model_runtime.sql",
        "0015_m5_learning_observation_audit_identity.sql",
        "0016_m1_m2_m3_capabilities.sql",
        "0017_vector_index_metadata.sql",
        "0018_m9_model_invocation_audits.sql",
        "0019_m7_model_invocation_audits.sql",
        "0020_m0_waiting_rooms.sql",
        "0021_m0_rescore_operation.sql",
        "0022_m0_dynamic_class_roster.sql",
    )
    all_sql = "\n".join(migration.sql for migration in migrations)
    for table_name in CORE_TABLES:
        assert f"CREATE TABLE {table_name}" in all_sql
    assert "m1_course_packages" not in all_sql
    assert "m2_evidence_indexes" not in all_sql
    assert "m3_knowledge_bundles" not in all_sql
    assert "TIMESTAMPTZ" in all_sql
    assert "JSONB" in all_sql
    assert "INTEGER" in all_sql
    assert "TEXT" in all_sql
    assert "CHAR(64)" in all_sql
    assert "BOOLEAN" in MIGRATION_TABLE_SQL


def test_m5_audit_identity_migration_backfills_one_guard_per_audit_version() -> None:
    """Catch PostgreSQL accepting concurrent aliases for one M5 audit."""

    migration = next(item for item in load_migrations() if item.version == 15)

    assert migration.name == "m5_learning_observation_audit_identity"
    assert "CREATE TABLE m5_learning_observation_audits" in migration.sql
    assert "PRIMARY KEY (source_audit_id, source_audit_version)" in migration.sql
    assert "DISTINCT ON" in migration.sql


def test_m4_intent_migration_is_checksum_locked_and_constraint_complete() -> None:
    migration = next(
        item for item in load_migrations() if item.version == 10
    )

    assert migration.version == 10
    assert migration.name == "m4_intent_decisions"
    assert migration.checksum == hashlib.sha256(
        migration.path.read_bytes()
    ).hexdigest()
    assert len(migration.checksum) == 64
    sql = migration.sql
    assert "CREATE TABLE m4_intent_decisions" in sql
    assert "request_key TEXT PRIMARY KEY" in sql
    assert "confidence DOUBLE PRECISION" in sql
    assert "margin DOUBLE PRECISION" in sql
    assert "reason_codes_json JSONB NOT NULL" in sql
    assert "shadow_json JSONB" in sql
    assert "created_at TIMESTAMPTZ NOT NULL" in sql
    assert "decision_status IN (" in sql
    assert "resolved_task_type IN (" in sql
    assert "decision_source IN (" in sql
    assert "jsonb_typeof(reason_codes_json) = 'array'" in sql
    assert "jsonb_typeof(shadow_json) = 'object'" in sql
    assert "schema_version = 1" in sql
    assert sql.count("~ '^[0-9a-f]{64}$'") == 2


def test_m4_runtime_status_migration_extends_the_database_constraint() -> None:
    migration = next(
        item for item in load_migrations() if item.version == 11
    )

    assert migration.version == 11
    assert migration.name == "m4_intent_runtime_statuses"
    assert migration.checksum == hashlib.sha256(
        migration.path.read_bytes()
    ).hexdigest()
    assert len(migration.checksum) == 64
    statements = tuple(
        statement.replace("\r\n", "\n")
        for statement in migration.statements
    )
    assert statements == (
        (
            "ALTER TABLE m4_intent_decisions\n"
            "    DROP CONSTRAINT "
            "m4_intent_decisions_decision_status_check"
        ),
        (
            "ALTER TABLE m4_intent_decisions\n"
            "    ADD CONSTRAINT "
            "m4_intent_decisions_decision_status_check\n"
            "    CHECK (\n"
            "        decision_status IN (\n"
            "            'accepted',\n"
            "            'abstained',\n"
            "            'out_of_scope',\n"
            "            'unavailable',\n"
            "            'failed',\n"
            "            'invalid'\n"
            "        )\n"
            "    )"
        ),
    )


def test_contract_payload_tables_store_checksum_and_schema_version() -> None:
    migration_sql = {
        migration.version: migration.sql for migration in load_migrations()
    }
    expected_counts = {
        2: 1,
        3: 3,
        4: 2,
        5: 1,
        6: 3,
        7: 2,
    }
    for version, count in expected_counts.items():
        sql = migration_sql[version]
        assert sql.count("payload_checksum CHAR(64) NOT NULL") == count
        assert sql.count("schema_version TEXT NOT NULL") == count
    assert "payload_checksum CHAR(64) NOT NULL" not in migration_sql[1]
    assert migration_sql[12].count("payload_checksum CHAR(64) NOT NULL") == 5
    assert "schema_version TEXT NOT NULL" not in migration_sql[12]
    assert "record TEXT NOT NULL" in migration_sql[1]
    assert "payload JSONB NOT NULL" in migration_sql[1]


def test_outbox_and_workflow_ddl_preserve_v7_lease_version_status_rules() -> None:
    m0_sql = load_migrations()[0].sql
    all_sql = "\n".join(migration.sql for migration in load_migrations())

    assert "status IN ('pending', 'processing', 'dead')" in m0_sql
    assert "attempt_count INTEGER NOT NULL" in m0_sql
    assert "version INTEGER NOT NULL" in m0_sql
    assert "available_at TIMESTAMPTZ NOT NULL" in m0_sql
    assert "locked_at TIMESTAMPTZ" in m0_sql
    assert "lease_until TIMESTAMPTZ" in m0_sql
    assert m0_sql.count("CHECK (updated_at >= created_at)") == 1
    assert "lease_until > locked_at" in m0_sql
    assert "ON DELETE CASCADE" in m0_sql
    assert "CREATE INDEX m0_outbox_claim_order" in m0_sql
    assert "CREATE UNIQUE INDEX m0_one_submit_per_paper" in m0_sql
    assert "WHERE operation = 'submit'" in m0_sql
    assert "CREATE UNIQUE INDEX m0_one_nonterminal_review_per_paper" in all_sql
    assert "WHERE operation = 'review' AND status <> 'completed'" in all_sql
    assert "ADD COLUMN knowledge_bundle_checksum CHAR(64)" in all_sql
    assert "ADD COLUMN previous_state_frozen BOOLEAN" in all_sql
    assert "m0_workflow_baseline_frozen" in all_sql
    assert "ADD COLUMN policy_id TEXT" in all_sql
    assert "m0_workflow_policy_identity_complete" in all_sql
    assert "request_checksum TEXT NOT NULL" in m0_sql
    assert "scoring_result_checksum CHAR(64)" in m0_sql


def test_run_migrations_locks_transaction_and_uses_parameterized_records(
    tmp_path: Path,
) -> None:
    _write_migration(tmp_path, 1, "first", "SELECT 1;")
    _write_migration(tmp_path, 2, "second", "SELECT 2;")
    connection = _FakeMigrationConnection()
    pool = _FakeMigrationPool(connection)

    first_report = run_migrations(pool, directory=tmp_path)
    second_report = run_migrations(pool, directory=tmp_path)

    assert first_report.applied_versions == (1, 2)
    assert first_report.current_version == 2
    assert second_report.applied_versions == ()
    assert second_report.current_version == 2
    assert connection.commits == 2
    assert connection.rollbacks == 0
    lock_calls = [
        call
        for call in connection.executions
        if call[0].startswith("SELECT pg_advisory_xact_lock")
    ]
    assert lock_calls == [
        ("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,), True),
        ("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,), True),
    ]
    insert_calls = [
        call
        for call in connection.executions
        if call[0].startswith("INSERT INTO schema_migrations")
    ]
    assert len(insert_calls) == 2
    assert all("%s" in query for query, _, _ in insert_calls)
    assert all(in_transaction for _, _, in_transaction in insert_calls)
    assert all(
        name not in query and checksum not in query
        for query, parameters, _ in insert_calls
        for _, name, checksum in [parameters]
    )


def test_run_migrations_rejects_future_drift_and_missing_applied_rows(
    tmp_path: Path,
) -> None:
    _write_migration(tmp_path, 1, "first", "SELECT 1;")
    _write_migration(tmp_path, 2, "second", "SELECT 2;")
    _write_migration(tmp_path, 3, "third", "SELECT 3;")
    migrations = load_migrations(tmp_path)

    cases = [
        (
            {4: ("future", "0" * 64)},
            "newer than this application",
        ),
        (
            {1: ("renamed", migrations[0].checksum)},
            "name drift",
        ),
        (
            {1: ("first", "f" * 64)},
            "checksum drift",
        ),
        (
            {
                1: ("first", migrations[0].checksum),
                3: ("third", migrations[2].checksum),
            },
            "missing applied migration version 2",
        ),
    ]
    for applied, message in cases:
        connection = _FakeMigrationConnection(applied=applied)
        with pytest.raises(PostgresMigrationError, match=message):
            run_migrations(
                _FakeMigrationPool(connection),
                directory=tmp_path,
            )
        assert connection.rollbacks == 1


def test_failed_migration_rolls_back_every_new_version_and_redacts_error(
    tmp_path: Path,
) -> None:
    _write_migration(tmp_path, 1, "first", "SELECT 1;")
    _write_migration(tmp_path, 2, "second", "SELECT FAIL_MIGRATION;")
    connection = _FakeMigrationConnection()
    connection.fail_on_statement = "FAIL_MIGRATION"
    connection.failure_text = f"driver exposed {_SECRET_DSN}"

    with pytest.raises(PostgresMigrationError) as captured:
        run_migrations(
            _FakeMigrationPool(connection),
            directory=tmp_path,
        )

    assert connection.applied == {}
    assert not connection.migration_table_exists
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert _SECRET_DSN not in str(captured.value)
    assert "private_password" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_current_schema_version_and_schema_is_current_handle_clean_database(
    tmp_path: Path,
) -> None:
    for version in range(1, 3):
        _write_migration(
            tmp_path,
            version,
            f"migration_{version}",
            f"SELECT {version};",
        )
    connection = _FakeMigrationConnection(migration_table_exists=False)
    pool = _FakeMigrationPool(connection)

    assert current_schema_version(pool, directory=tmp_path) == 0
    assert not schema_is_current(pool, directory=tmp_path)
    run_migrations(pool, directory=tmp_path)
    assert current_schema_version(pool, directory=tmp_path) == 2
    assert schema_is_current(pool, directory=tmp_path)


def test_current_schema_version_rejects_future_drift_and_missing_rows() -> None:
    migrations = load_migrations()
    cases = (
        {SCHEMA_VERSION + 1: ("future", "0" * 64)},
        {1: ("renamed", migrations[0].checksum)},
        {1: ("m0_platform", "f" * 64)},
        {
            1: ("m0_platform", migrations[0].checksum),
            3: ("m5_learner_class_state", migrations[2].checksum),
        },
    )
    for applied in cases:
        pool = _FakeMigrationPool(_FakeMigrationConnection(applied=applied))
        with pytest.raises(PostgresMigrationError):
            current_schema_version(pool)
        assert not schema_is_current(pool)


def test_test_schema_destruction_requires_explicit_opt_in_and_rebuilds(
    tmp_path: Path,
) -> None:
    _write_migration(tmp_path, 1, "first", "SELECT 1;")
    connection = _FakeMigrationConnection(
        applied={1: ("first", load_migrations(tmp_path)[0].checksum)}
    )
    pool = _FakeMigrationPool(connection)

    with pytest.raises(ValueError, match="explicit destructive opt-in"):
        destroy_schema_for_tests(pool)
    assert connection.executions == []

    report = rebuild_schema_for_tests(
        pool,
        allow_destructive=True,
        directory=tmp_path,
    )

    assert report.applied_versions == (1,)
    assert report.current_version == 1
    drop_calls = [
        query
        for query, _, _ in connection.executions
        if query.startswith("DROP TABLE IF EXISTS")
    ]
    assert drop_calls == [
        *(f"DROP TABLE IF EXISTS {table}" for table in CORE_TABLES),
        "DROP TABLE IF EXISTS schema_migrations",
    ]
    assert all(";" not in query for query in drop_calls)
