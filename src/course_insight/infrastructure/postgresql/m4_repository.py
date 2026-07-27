"""PostgreSQL persistence for replay-safe M4 task plans."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.modules.m4_task_orchestration.intent import IntentStatus
from course_insight.modules.m4_task_orchestration.intent_service import (
    StoredIntentDecision,
)


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

    def get_intent_decision(
        self,
        request_key: str,
    ) -> StoredIntentDecision | None:
        """Load and verify one private decision by its exact request key."""

        try:
            with self._pool.connection() as connection:
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
                    WHERE request_key = %s
                    """,
                    (request_key,),
                ).fetchone()
                return None if row is None else _intent_decision_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_intent_decision(
        self,
        decision: StoredIntentDecision,
    ) -> StoredIntentDecision:
        """Insert one decision or return the verified first-writer winner."""

        candidate = _isolated_intent_decision(decision)
        shadow = candidate.shadow_payload()
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
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
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (request_key) DO NOTHING
                        RETURNING
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
                        """,
                        (
                            candidate.request_key,
                            candidate.resolved_task_type,
                            candidate.decision_status.value,
                            candidate.decision_source,
                            candidate.adapter_id,
                            candidate.adapter_version,
                            candidate.policy_version,
                            candidate.confidence,
                            candidate.margin,
                            candidate.input_checksum,
                            Jsonb(list(candidate.reason_codes)),
                            None if shadow is None else Jsonb(shadow),
                            candidate.schema_version,
                            candidate.payload_checksum,
                            candidate.created_at,
                        ),
                    ).fetchone()
                    if row is None:
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
                            WHERE request_key = %s
                            """,
                            (candidate.request_key,),
                        ).fetchone()
                    if row is None:
                        raise PostgresOperationError(_INTEGRITY_ERROR)
                    return _intent_decision_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _isolated_task_plan(plan: TaskPlan) -> TaskPlan:
    if not isinstance(plan, TaskPlan):
        raise TypeError("plan must be a TaskPlan")
    return TaskPlan.model_validate(plan.model_dump(mode="python"))


def _isolated_intent_decision(
    decision: StoredIntentDecision,
) -> StoredIntentDecision:
    if not isinstance(decision, StoredIntentDecision):
        raise TypeError("decision must be a StoredIntentDecision")
    try:
        decision.assert_integrity()
        return _copy_intent_decision(decision)
    except (TypeError, ValueError) as error:
        raise ValueError("M4 intent decision is invalid") from error


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


def _intent_decision_from_row(row: Any) -> StoredIntentDecision:
    try:
        reason_codes = row["reason_codes_json"]
        if type(reason_codes) is not list:
            raise ValueError("reason codes must be a JSON array")
        shadow = row["shadow_json"]
        if shadow is not None:
            if (
                type(shadow) is not dict
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
                or type(shadow["reason_codes"]) is not list
            ):
                raise ValueError("shadow metadata must be canonical")
        created_at = row["created_at"]
        if (
            not isinstance(created_at, datetime)
            or created_at.tzinfo is None
            or created_at.utcoffset() is None
        ):
            raise ValueError("created_at must be timezone-aware")
        stored = StoredIntentDecision(
            request_key=_required_string(row, "request_key"),
            resolved_task_type=_optional_string(row, "resolved_task_type"),
            decision_status=IntentStatus(
                _required_string(row, "decision_status")
            ),
            decision_source=_required_string(row, "decision_source"),
            adapter_id=_required_string(row, "adapter_id"),
            adapter_version=_required_string(row, "adapter_version"),
            policy_version=_required_string(row, "policy_version"),
            confidence=row["confidence"],
            margin=row["margin"],
            input_checksum=_required_sha256(row, "input_checksum"),
            reason_codes=tuple(reason_codes),
            created_at=created_at.astimezone(timezone.utc),
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
            schema_version=row["schema_version"],
            payload_checksum=_required_sha256(row, "payload_checksum"),
        )
        stored.assert_persisted_integrity()
        return stored
    except PostgresOperationError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _copy_intent_decision(
    decision: StoredIntentDecision,
) -> StoredIntentDecision:
    return StoredIntentDecision(
        request_key=decision.request_key,
        resolved_task_type=decision.resolved_task_type,
        decision_status=decision.decision_status,
        decision_source=decision.decision_source,
        adapter_id=decision.adapter_id,
        adapter_version=decision.adapter_version,
        policy_version=decision.policy_version,
        confidence=decision.confidence,
        margin=decision.margin,
        input_checksum=decision.input_checksum,
        reason_codes=tuple(decision.reason_codes),
        created_at=decision.created_at,
        shadow_label=decision.shadow_label,
        shadow_status=decision.shadow_status,
        shadow_adapter_id=decision.shadow_adapter_id,
        shadow_adapter_version=decision.shadow_adapter_version,
        shadow_confidence=decision.shadow_confidence,
        shadow_margin=decision.shadow_margin,
        shadow_reason_codes=tuple(decision.shadow_reason_codes),
        shadow_agrees=decision.shadow_agrees,
        schema_version=decision.schema_version,
        payload_checksum=decision.payload_checksum,
    )


def _required_string(row: Any, field: str) -> str:
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _optional_string(row: Any, field: str) -> str | None:
    return None if row[field] is None else _required_string(row, field)


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
