"""SQLite implementation of the M4 task-plan persistence boundary."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


class SQLiteM4Repository:
    """Persist replay-safe M4 plans without leaking SQLite into M4 services."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        """Create the database and apply all pending migrations."""

        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def insert_or_get_task_plan(
        self,
        plan: TaskPlan,
        idempotency_key: str,
    ) -> TaskPlan:
        """Insert one plan or return the winner of an idempotency race."""

        self._validate_storage_identity(plan, idempotency_key)
        payload = dumps_json(plan.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m4_task_plans(task_id, idempotency_key, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (plan.task_id, idempotency_key, payload),
            )
            row = connection.execute(
                """
                SELECT task_id, idempotency_key, payload
                FROM m4_task_plans
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("M4 task insert produced no authoritative row")
            stored = self._task_plan_from_row(row)
            connection.execute("COMMIT")
            return stored
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_task_plan(self, plan: TaskPlan, idempotency_key: str) -> None:
        """Retain the legacy write shape while preserving insert-or-get rules."""

        self.insert_or_get_task_plan(plan, idempotency_key)

    def get_task_plan(self, task_id: str) -> TaskPlan | None:
        """Load one isolated task plan by its stable identity."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT task_id, idempotency_key, payload
                FROM m4_task_plans
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            return None if row is None else self._task_plan_from_row(row)
        finally:
            connection.close()

    @staticmethod
    def _task_plan_from_row(row: sqlite3.Row) -> TaskPlan:
        task_id = str(row["task_id"])
        idempotency_key = str(row["idempotency_key"])
        plan = TaskPlan.model_validate_json(str(row["payload"]))
        if plan.task_id != task_id or task_id != f"task_{idempotency_key}":
            raise RuntimeError("M4 task row identity does not match its payload")
        return plan

    @staticmethod
    def _validate_storage_identity(
        plan: TaskPlan,
        idempotency_key: str,
    ) -> None:
        if not idempotency_key or plan.task_id != f"task_{idempotency_key}":
            raise ValueError("M4 task identity must derive from its idempotency key")
