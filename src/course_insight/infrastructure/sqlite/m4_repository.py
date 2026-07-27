"""SQLite implementation of the M4 task-plan persistence boundary."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.modules.m4_task_orchestration.intent import IntentStatus
from course_insight.modules.m4_task_orchestration.intent_service import (
    StoredIntentDecision,
)


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

    def get_intent_decision(
        self,
        request_key: str,
    ) -> StoredIntentDecision | None:
        """Load and verify one private decision by its exact request key."""

        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    request_key,
                    resolved_task_type,
                    decision_status,
                    decision_source,
                    adapter_id,
                    adapter_version,
                    policy_version,
                    confidence,
                    margin,
                    input_checksum,
                    reason_codes_json,
                    shadow_json,
                    schema_version,
                    payload_checksum,
                    created_at
                FROM m4_intent_decisions
                WHERE request_key = ?
                """,
                (request_key,),
            ).fetchone()
            return None if row is None else self._intent_decision_from_row(row)
        finally:
            connection.close()

    def insert_or_get_intent_decision(
        self,
        decision: StoredIntentDecision,
    ) -> StoredIntentDecision:
        """Insert one decision or return the verified first-writer winner."""

        self._validate_intent_candidate(decision)
        reason_codes_json = dumps_json(list(decision.reason_codes))
        shadow_payload = decision.shadow_payload()
        shadow_json = (
            None if shadow_payload is None else dumps_json(shadow_payload)
        )
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO m4_intent_decisions(
                    request_key,
                    resolved_task_type,
                    decision_status,
                    decision_source,
                    adapter_id,
                    adapter_version,
                    policy_version,
                    confidence,
                    margin,
                    input_checksum,
                    reason_codes_json,
                    shadow_json,
                    schema_version,
                    payload_checksum,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_key) DO NOTHING
                """,
                (
                    decision.request_key,
                    decision.resolved_task_type,
                    decision.decision_status.value,
                    decision.decision_source,
                    decision.adapter_id,
                    decision.adapter_version,
                    decision.policy_version,
                    decision.confidence,
                    decision.margin,
                    decision.input_checksum,
                    reason_codes_json,
                    shadow_json,
                    decision.schema_version,
                    decision.payload_checksum,
                    decision.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                """
                SELECT
                    request_key,
                    resolved_task_type,
                    decision_status,
                    decision_source,
                    adapter_id,
                    adapter_version,
                    policy_version,
                    confidence,
                    margin,
                    input_checksum,
                    reason_codes_json,
                    shadow_json,
                    schema_version,
                    payload_checksum,
                    created_at
                FROM m4_intent_decisions
                WHERE request_key = ?
                """,
                (decision.request_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "M4 intent insert produced no authoritative row"
                )
            stored = self._intent_decision_from_row(row)
            connection.execute("COMMIT")
            return stored
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
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

    @staticmethod
    def _validate_intent_candidate(decision: StoredIntentDecision) -> None:
        if not isinstance(decision, StoredIntentDecision):
            raise TypeError("M4 intent decision must be a StoredIntentDecision")
        try:
            decision.assert_integrity()
        except (TypeError, ValueError) as error:
            raise ValueError("M4 intent decision is invalid") from error

    @staticmethod
    def _intent_decision_from_row(row: sqlite3.Row) -> StoredIntentDecision:
        try:
            reason_codes_text = str(row["reason_codes_json"])
            reason_codes = json.loads(reason_codes_text)
            if (
                not isinstance(reason_codes, list)
                or dumps_json(reason_codes) != reason_codes_text
            ):
                raise ValueError("intent reason codes are not canonical")

            shadow_text = row["shadow_json"]
            shadow = None if shadow_text is None else json.loads(str(shadow_text))
            if shadow is not None and (
                not isinstance(shadow, dict)
                or set(shadow) != {
                    "adapter_id",
                    "adapter_version",
                    "agrees",
                    "confidence",
                    "label",
                    "margin",
                    "reason_codes",
                    "status",
                }
                or not isinstance(shadow["reason_codes"], list)
                or dumps_json(shadow) != str(shadow_text)
            ):
                raise ValueError("intent shadow metadata is not canonical")

            created_at_text = str(row["created_at"])
            created_at = datetime.fromisoformat(created_at_text)
            if created_at.isoformat() != created_at_text:
                raise ValueError("intent creation time is not canonical")

            stored = StoredIntentDecision(
                request_key=str(row["request_key"]),
                resolved_task_type=(
                    None
                    if row["resolved_task_type"] is None
                    else str(row["resolved_task_type"])
                ),
                decision_status=IntentStatus(str(row["decision_status"])),
                decision_source=str(row["decision_source"]),
                adapter_id=str(row["adapter_id"]),
                adapter_version=str(row["adapter_version"]),
                policy_version=str(row["policy_version"]),
                confidence=row["confidence"],
                margin=row["margin"],
                input_checksum=str(row["input_checksum"]),
                reason_codes=tuple(reason_codes),
                created_at=created_at,
                shadow_label=None if shadow is None else shadow["label"],
                shadow_status=(
                    None
                    if shadow is None
                    else IntentStatus(str(shadow["status"]))
                ),
                shadow_adapter_id=(
                    None if shadow is None else shadow["adapter_id"]
                ),
                shadow_adapter_version=(
                    None if shadow is None else shadow["adapter_version"]
                ),
                shadow_confidence=(
                    None if shadow is None else shadow["confidence"]
                ),
                shadow_margin=None if shadow is None else shadow["margin"],
                shadow_reason_codes=(
                    () if shadow is None else tuple(shadow["reason_codes"])
                ),
                shadow_agrees=None if shadow is None else shadow["agrees"],
                schema_version=int(row["schema_version"]),
                payload_checksum=str(row["payload_checksum"]),
            )
            stored.assert_persisted_integrity()
            return stored
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                "M4 persisted intent decision is corrupt"
            ) from error
