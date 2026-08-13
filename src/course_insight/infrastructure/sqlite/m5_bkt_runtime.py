"""Focused SQLite persistence for BKT models and knowledge traces."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from course_insight.contracts.learning_models import (
    BktModelArtifact,
    KnowledgeTraceSnapshot,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite


def insert_or_get_bkt_model(
    database_path: Path,
    model: BktModelArtifact,
) -> BktModelArtifact:
    """Insert one append-only BKT model version."""

    candidate = BktModelArtifact.model_validate(model.model_dump(mode="python"))
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO m5_bkt_models (
                model_id,
                course_id,
                model_version,
                created_at,
                payload,
                payload_checksum,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.model_id,
                candidate.course_id,
                candidate.model_version,
                candidate.created_at.isoformat(),
                dumps_json(candidate.to_dict()),
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
            WHERE model_id = ? OR (course_id = ? AND model_version = ?)
            ORDER BY model_id = ? DESC
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
            raise RuntimeError("M5 BKT model conflict for the same identity")
        connection.execute("COMMIT")
        return stored.model_copy(deep=True)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_bkt_model(
    database_path: Path,
    *,
    course_id: str,
    model_version: str,
) -> BktModelArtifact | None:
    """Load one exact course-scoped BKT model version."""

    connection = connect_sqlite(database_path)
    try:
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
            WHERE course_id = ? AND model_version = ?
            """,
            (course_id, model_version),
        ).fetchone()
        return None if row is None else _model_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def get_latest_bkt_model(
    database_path: Path,
    *,
    course_id: str,
    class_id: str,
) -> BktModelArtifact | None:
    """Load the latest BKT model for one teaching scope."""

    connection = connect_sqlite(database_path)
    try:
        rows = connection.execute(
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
            WHERE course_id = ?
            ORDER BY created_at DESC, model_id DESC
            """,
            (course_id,),
        ).fetchall()
        for row in rows:
            model = _model_from_row(row)
            if model.class_id == class_id:
                return model.model_copy(deep=True)
        return None
    finally:
        connection.close()


def insert_or_get_knowledge_trace(
    database_path: Path,
    trace: KnowledgeTraceSnapshot,
) -> KnowledgeTraceSnapshot:
    """Insert one immutable BKT knowledge-trace snapshot."""

    candidate = KnowledgeTraceSnapshot.model_validate(
        trace.model_dump(mode="python")
    )
    if candidate.course_id is None or candidate.class_id is None:
        raise ValueError("persisted BKT traces require course and class scope")
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO m5_knowledge_traces (
                trace_id,
                course_id,
                class_id,
                learner_id,
                model_version,
                updated_at,
                payload,
                payload_checksum,
                schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.trace_id,
                candidate.course_id,
                candidate.class_id,
                candidate.learner_id,
                candidate.model_version,
                candidate.updated_at.isoformat(),
                dumps_json(candidate.to_dict()),
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
            WHERE trace_id = ?
            """,
            (candidate.trace_id,),
        ).fetchone()
        stored = _trace_from_row(row)
        if stored != candidate:
            raise RuntimeError("M5 BKT trace conflict for the same identity")
        connection.execute("COMMIT")
        return stored.model_copy(deep=True)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_knowledge_trace(
    database_path: Path,
    *,
    trace_id: str,
) -> KnowledgeTraceSnapshot | None:
    """Load one exact knowledge-trace snapshot."""

    connection = connect_sqlite(database_path)
    try:
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
            WHERE trace_id = ?
            """,
            (trace_id,),
        ).fetchone()
        return None if row is None else _trace_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def _model_from_row(row: sqlite3.Row | None) -> BktModelArtifact:
    if row is None:
        raise RuntimeError("M5 BKT model insert produced no row")
    model = BktModelArtifact.model_validate_json(str(row["payload"]))
    if (
        model.model_id != str(row["model_id"])
        or model.course_id != str(row["course_id"])
        or model.model_version != str(row["model_version"])
        or model.created_at.isoformat() != str(row["created_at"])
        or model.content_checksum() != str(row["payload_checksum"])
        or model.schema_version != str(row["schema_version"])
    ):
        raise RuntimeError("M5 BKT model row integrity mismatch")
    return model


def _trace_from_row(row: sqlite3.Row | None) -> KnowledgeTraceSnapshot:
    if row is None:
        raise RuntimeError("M5 BKT trace insert produced no row")
    trace = KnowledgeTraceSnapshot.model_validate_json(str(row["payload"]))
    if (
        trace.trace_id != str(row["trace_id"])
        or trace.course_id != str(row["course_id"])
        or trace.class_id != str(row["class_id"])
        or trace.learner_id != str(row["learner_id"])
        or trace.model_version != str(row["model_version"])
        or trace.updated_at.isoformat() != str(row["updated_at"])
        or trace.content_checksum() != str(row["payload_checksum"])
        or trace.schema_version != str(row["schema_version"])
    ):
        raise RuntimeError("M5 BKT trace row integrity mismatch")
    return trace
