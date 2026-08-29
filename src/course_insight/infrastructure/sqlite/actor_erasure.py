"""Bounded physical actor erasure shared by SQLite module repositories."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from course_insight.infrastructure.sqlite.connection import connect_sqlite


_ACTOR = re.compile(r"^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$")
_MODULES = frozenset({"m0", "m4", "m5", "m6", "m7", "m8", "m9"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ACTOR_COLUMNS = frozenset(
    {"actor_id", "learner_id", "reviewer_id", "requester_id"}
)
_DELETE_PRIORITY = {
    "m9_model_invocation_audits": 0,
    "m9_teacher_reviews": 1,
    "m9_teacher_analytics": 2,
    "m8_score_audits": 0,
    "m8_scoring_results": 1,
    "m8_assessment_papers": 2,
    "m0_event_outbox": 0,
    "m0_learning_events": 1,
    "m0_assessment_runs": 2,
}


def purge_sqlite_actor(
    database_path: Path,
    *,
    module: str,
    actor_id: str,
) -> int:
    """Delete rows owned by one module that contain an exact actor value."""

    if module not in _MODULES or not isinstance(actor_id, str) or not _ACTOR.fullmatch(actor_id):
        raise ValueError("actor erasure scope is invalid")
    connection = connect_sqlite(database_path)
    before = connection.total_changes
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = _module_tables(connection, module)
        if module == "m9":
            _purge_m9_dependants(connection, actor_id, tables)
        if module == "m8":
            _purge_m8_audits(connection, actor_id, tables)
        for table in sorted(
            tables,
            key=lambda name: (_DELETE_PRIORITY.get(name, 1), name),
        ):
            _delete_actor_rows(connection, table, actor_id)
        connection.execute("COMMIT")
        return connection.total_changes - before
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _module_tables(connection: sqlite3.Connection, module: str) -> tuple[str, ...]:
    prefix = f"{module}\\_%"
    return tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ? ESCAPE '\\'",
            (prefix,),
        ).fetchall()
        if _IDENTIFIER.fullmatch(str(row[0]))
    )


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("unsafe SQLite table identifier")
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if _IDENTIFIER.fullmatch(str(row[1]))
    )


def _delete_actor_rows(
    connection: sqlite3.Connection,
    table: str,
    actor_id: str,
) -> None:
    columns = _columns(connection, table)
    conditions: list[str] = []
    parameters: list[str] = []
    for column in columns:
        quoted = f'"{column}"'
        if column in _ACTOR_COLUMNS:
            conditions.append(f"{quoted} = ?")
            parameters.append(actor_id)
        conditions.append(
            f"(json_valid({quoted}) AND EXISTS ("
            f"SELECT 1 FROM json_tree({quoted}) WHERE atom = ?))"
        )
        parameters.append(actor_id)
    if conditions:
        connection.execute(
            f'DELETE FROM "{table}" WHERE ' + " OR ".join(conditions),
            tuple(parameters),
        )


def _purge_m9_dependants(
    connection: sqlite3.Connection,
    actor_id: str,
    tables: tuple[str, ...],
) -> None:
    if {
        "m9_model_invocation_audits",
        "m9_teacher_analytics",
    }.issubset(tables):
        connection.execute(
            """
            DELETE FROM m9_model_invocation_audits
            WHERE source_report_id IN (
                SELECT report_id
                FROM m9_teacher_analytics
                WHERE (
                    json_valid(learner_ids)
                    AND EXISTS (
                        SELECT 1 FROM json_tree(learner_ids) WHERE atom = ?
                    )
                ) OR (
                    json_valid(payload)
                    AND EXISTS (
                        SELECT 1 FROM json_tree(payload) WHERE atom = ?
                    )
                )
            )
            """,
            (actor_id, actor_id),
        )


def _purge_m8_audits(
    connection: sqlite3.Connection,
    actor_id: str,
    tables: tuple[str, ...],
) -> None:
    if {"m8_score_audits", "m8_scoring_results"}.issubset(tables):
        connection.execute(
            """
            DELETE FROM m8_score_audits
            WHERE audit_id IN (
                SELECT DISTINCT CAST(tree.atom AS TEXT)
                FROM m8_scoring_results AS result,
                     json_tree(result.payload) AS tree
                WHERE result.learner_id = ? AND tree.key = 'audit_id'
            )
            """,
            (actor_id,),
        )


__all__ = ["purge_sqlite_actor"]
