"""Focused PostgreSQL persistence for BKT models and knowledge traces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.base import ContractModel
from course_insight.contracts.learning_models import (
    BktModelArtifact,
    KnowledgeTraceSnapshot,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool


_TContract = TypeVar("_TContract", bound=ContractModel)
_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M5 repository integrity check failed"
_CONFLICT_ERROR = "PostgreSQL M5 repository identity conflict"


def insert_or_get_bkt_model(
    pool: PostgresPool,
    model: BktModelArtifact,
) -> BktModelArtifact:
    """Insert one append-only BKT model version."""

    candidate = _isolated(model, BktModelArtifact)
    try:
        with pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO m5_bkt_models(
                        model_id,
                        course_id,
                        model_version,
                        created_at,
                        payload,
                        payload_checksum,
                        schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        candidate.model_id,
                        candidate.course_id,
                        candidate.model_version,
                        candidate.created_at,
                        Jsonb(candidate.to_dict()),
                        candidate.content_checksum(),
                        candidate.schema_version,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT
                        model_id,
                        course_id,
                        model_version,
                        created_at,
                        payload,
                        payload_checksum,
                        schema_version
                    FROM m5_bkt_models
                    WHERE model_id = %s
                       OR (course_id = %s AND model_version = %s)
                    ORDER BY (model_id = %s) DESC
                    LIMIT 1
                    """,
                    (
                        candidate.model_id,
                        candidate.course_id,
                        candidate.model_version,
                        candidate.model_id,
                    ),
                ).fetchone()
                stored = _model_from_row(row)
                if stored != candidate:
                    raise PostgresOperationError(_CONFLICT_ERROR)
                return stored
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_bkt_model(
    pool: PostgresPool,
    *,
    course_id: str,
    model_version: str,
) -> BktModelArtifact | None:
    """Load one exact course-scoped BKT model version."""

    try:
        with pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    model_id,
                    course_id,
                    model_version,
                    created_at,
                    payload,
                    payload_checksum,
                    schema_version
                FROM m5_bkt_models
                WHERE course_id = %s AND model_version = %s
                """,
                (course_id, model_version),
            ).fetchone()
            return None if row is None else _model_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def insert_or_get_knowledge_trace(
    pool: PostgresPool,
    trace: KnowledgeTraceSnapshot,
) -> KnowledgeTraceSnapshot:
    """Insert one immutable learner trace snapshot."""

    candidate = _isolated(trace, KnowledgeTraceSnapshot)
    if candidate.course_id is None or candidate.class_id is None:
        raise ValueError("persisted BKT traces require course and class scope")
    try:
        with pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO m5_knowledge_traces(
                        trace_id,
                        course_id,
                        class_id,
                        learner_id,
                        model_version,
                        updated_at,
                        payload,
                        payload_checksum,
                        schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trace_id) DO NOTHING
                    """,
                    (
                        candidate.trace_id,
                        candidate.course_id,
                        candidate.class_id,
                        candidate.learner_id,
                        candidate.model_version,
                        candidate.updated_at,
                        Jsonb(candidate.to_dict()),
                        candidate.content_checksum(),
                        candidate.schema_version,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT
                        trace_id,
                        course_id,
                        class_id,
                        learner_id,
                        model_version,
                        updated_at,
                        payload,
                        payload_checksum,
                        schema_version
                    FROM m5_knowledge_traces
                    WHERE trace_id = %s
                    """,
                    (candidate.trace_id,),
                ).fetchone()
                stored = _trace_from_row(row)
                if stored != candidate:
                    raise PostgresOperationError(_CONFLICT_ERROR)
                return stored
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_knowledge_trace(
    pool: PostgresPool,
    *,
    trace_id: str,
) -> KnowledgeTraceSnapshot | None:
    """Load one exact BKT trace snapshot."""

    try:
        with pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    trace_id,
                    course_id,
                    class_id,
                    learner_id,
                    model_version,
                    updated_at,
                    payload,
                    payload_checksum,
                    schema_version
                FROM m5_knowledge_traces
                WHERE trace_id = %s
                """,
                (trace_id,),
            ).fetchone()
            return None if row is None else _trace_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def _model_from_row(row: Mapping[str, Any] | None) -> BktModelArtifact:
    model = _contract_from_row(row, BktModelArtifact)
    if (
        model.model_id != _required_text(row, "model_id")
        or model.course_id != _required_text(row, "course_id")
        or model.model_version != _required_text(row, "model_version")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return model


def _trace_from_row(row: Mapping[str, Any] | None) -> KnowledgeTraceSnapshot:
    trace = _contract_from_row(row, KnowledgeTraceSnapshot)
    if (
        trace.trace_id != _required_text(row, "trace_id")
        or trace.course_id != _required_text(row, "course_id")
        or trace.class_id != _required_text(row, "class_id")
        or trace.learner_id != _required_text(row, "learner_id")
        or trace.model_version != _required_text(row, "model_version")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return trace


def _contract_from_row(
    row: Mapping[str, Any] | None,
    contract_type: type[_TContract],
) -> _TContract:
    if row is None or type(row.get("payload")) is not dict:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    contract = contract_type.model_validate(row["payload"])
    if (
        contract.content_checksum() != _required_text(row, "payload_checksum")
        or contract.schema_version != _required_text(row, "schema_version")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return contract


def _isolated(value: _TContract, contract_type: type[_TContract]) -> _TContract:
    if not isinstance(value, contract_type):
        raise TypeError(f"value must be a {contract_type.__name__}")
    return contract_type.model_validate(value.model_dump(mode="python"))


def _required_text(row: Mapping[str, Any] | None, field: str) -> str:
    if row is None:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    value = row[field]
    if type(value) is not str or not value:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return value
