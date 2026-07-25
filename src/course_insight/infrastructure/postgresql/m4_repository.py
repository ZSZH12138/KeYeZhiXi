"""PostgreSQL persistence for replay-safe M4 task plans."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool


_EXPECTED_SCHEMA_VERSION = str(
    TaskPlan.model_fields["schema_version"].default
)
_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M4 repository integrity check failed"


class PostgresM4Repository:
    """Persist M4 plans with PostgreSQL insert-or-get semantics."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def initialize(self) -> None:
        """Apply every verified PostgreSQL migration."""

        from course_insight.infrastructure.postgresql.migration_runner import (
            run_migrations,
        )

        run_migrations(self._pool)

    def insert_or_get_task_plan(
        self,
        plan: TaskPlan,
        idempotency_key: str,
    ) -> TaskPlan:
        """Insert a plan or return the first committed replay winner."""

        candidate = _isolated_task_plan(plan)
        _validate_storage_identity(candidate, idempotency_key)
        checksum = candidate.content_checksum()
        try:
            with self._pool.connection() as connection:
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
                        ON CONFLICT (idempotency_key) DO NOTHING
                        """,
                        (
                            candidate.task_id,
                            idempotency_key,
                            Jsonb(candidate.to_dict()),
                            checksum,
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT
                            task_id,
                            idempotency_key,
                            payload,
                            payload_checksum,
                            schema_version
                        FROM m4_task_plans
                        WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    if row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    return _task_plan_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_task_plan(self, plan: TaskPlan, idempotency_key: str) -> None:
        """Retain the legacy write shape while preserving insert-or-get."""

        self.insert_or_get_task_plan(plan, idempotency_key)

    def get_task_plan(self, task_id: str) -> TaskPlan | None:
        """Load one isolated task plan by stable identity."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT
                        task_id,
                        idempotency_key,
                        payload,
                        payload_checksum,
                        schema_version
                    FROM m4_task_plans
                    WHERE task_id = %s
                    """,
                    (task_id,),
                ).fetchone()
                return None if row is None else _task_plan_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _isolated_task_plan(plan: TaskPlan) -> TaskPlan:
    if not isinstance(plan, TaskPlan):
        raise TypeError("plan must be a TaskPlan")
    return TaskPlan.model_validate(plan.model_dump(mode="python"))


def _task_plan_from_row(row: Any) -> TaskPlan:
    try:
        task_id = _required_string(row, "task_id")
        idempotency_key = _required_string(row, "idempotency_key")
        payload = row["payload"]
        if type(payload) is not dict:
            raise ValueError("payload must be a JSON object")
        plan = TaskPlan.model_validate(payload)
        stored_checksum = _required_sha256(row, "payload_checksum")
        stored_schema_version = _required_string(row, "schema_version")
        if (
            plan.task_id != task_id
            or task_id != f"task_{idempotency_key}"
            or plan.schema_version != _EXPECTED_SCHEMA_VERSION
            or stored_schema_version != _EXPECTED_SCHEMA_VERSION
            or stored_schema_version != plan.schema_version
            or plan.content_checksum() != stored_checksum
        ):
            raise ValueError("stored task plan identity is inconsistent")
        return plan
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _required_string(row: Any, field: str) -> str:
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _required_sha256(row: Any, field: str) -> str:
    value = _required_string(row, field)
    if not _is_sha256(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_storage_identity(
    plan: TaskPlan,
    idempotency_key: str,
) -> None:
    if not _is_sha256(idempotency_key):
        raise ValueError(
            "M4 idempotency key must be a lowercase SHA-256 digest"
        )
    if plan.task_id != f"task_{idempotency_key}":
        raise ValueError("M4 task identity must derive from its idempotency key")
    if plan.schema_version != _EXPECTED_SCHEMA_VERSION:
        raise ValueError("M4 task schema version is unsupported")


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


PostgreSQLM4Repository = PostgresM4Repository

__all__ = [
    "PostgresM4Repository",
    "PostgresOperationError",
    "PostgreSQLM4Repository",
]
