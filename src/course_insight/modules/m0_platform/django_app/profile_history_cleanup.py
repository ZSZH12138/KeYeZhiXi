"""One-time cleanup for assessment artifacts outside formal profile evidence."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable


_PROFILE_HISTORY_TABLES = frozenset(
    {
        "m8_assessment_papers",
        "m8_frozen_assessment_records",
        "m8_scoring_results",
        "m8_score_audits",
        "m0_assessment_runs",
        "m7_student_feedback",
        "m4_task_plans",
    }
)


def cleanup_runtime_history(
    *,
    database_path: Path,
    keep_paper_ids: frozenset[str],
    apply: bool,
) -> dict[str, int]:
    """Keep only papers backed by completed profile projection receipts."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise ValueError(f"runtime database does not exist: {path}")
    connection = sqlite3.connect(path)
    try:
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if str(row[0]) in _PROFILE_HISTORY_TABLES
        }
        if "m8_assessment_papers" not in existing:
            return _empty_summary()
        paper_rows = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT paper_id, task_id FROM m8_assessment_papers"
            )
        )
        stale_rows = tuple(
            row for row in paper_rows if row[0] not in keep_paper_ids
        )
        stale_papers = tuple(row[0] for row in stale_rows)
        stale_tasks = tuple(row[1] for row in stale_rows)
        stale_attempts = _stale_attempt_ids(
            connection,
            existing=existing,
            stale_papers=stale_papers,
        )
        summary = {
            "kept_profile_papers": len(paper_rows) - len(stale_rows),
            "removed_papers": len(stale_rows),
            "removed_attempts": len(stale_attempts),
            "removed_tasks": len(set(stale_tasks)),
        }
        if not apply or not stale_rows:
            return summary
        connection.execute("BEGIN IMMEDIATE")
        if "m8_score_audits" in existing:
            _delete_where_in(
                connection,
                "m8_score_audits",
                "json_extract(payload, '$.attempt_id')",
                stale_attempts,
            )
        if "m8_scoring_results" in existing:
            _delete_where_in(
                connection,
                "m8_scoring_results",
                "paper_id",
                stale_papers,
            )
        if "m0_assessment_runs" in existing:
            _delete_where_in(
                connection,
                "m0_assessment_runs",
                "paper_id",
                stale_papers,
            )
        if "m7_student_feedback" in existing:
            _delete_where_in(
                connection,
                "m7_student_feedback",
                "task_id",
                stale_tasks,
            )
        if "m8_frozen_assessment_records" in existing:
            _delete_where_in(
                connection,
                "m8_frozen_assessment_records",
                "paper_id",
                stale_papers,
            )
        _delete_where_in(
            connection,
            "m8_assessment_papers",
            "paper_id",
            stale_papers,
        )
        if "m4_task_plans" in existing:
            _delete_where_in(
                connection,
                "m4_task_plans",
                "task_id",
                stale_tasks,
            )
        connection.execute("COMMIT")
        return summary
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _stale_attempt_ids(
    connection: sqlite3.Connection,
    *,
    existing: set[str],
    stale_papers: tuple[str, ...],
) -> tuple[str, ...]:
    attempts: set[str] = set()
    if "m8_scoring_results" in existing:
        attempts.update(
            str(row[0])
            for row in _select_where_in(
                connection,
                "m8_scoring_results",
                "attempt_id",
                "paper_id",
                stale_papers,
            )
            if row[0]
        )
    if "m0_assessment_runs" in existing:
        attempts.update(
            str(row[0])
            for row in _select_where_in(
                connection,
                "m0_assessment_runs",
                "attempt_id",
                "paper_id",
                stale_papers,
            )
            if row[0]
        )
    return tuple(sorted(attempts))


def _select_where_in(
    connection: sqlite3.Connection,
    table: str,
    selected_column: str,
    filtered_column: str,
    values: tuple[str, ...],
) -> tuple[sqlite3.Row, ...]:
    if not values:
        return ()
    placeholders = ",".join("?" for _ in values)
    return tuple(
        connection.execute(
            f"SELECT {selected_column} FROM {table} "
            f"WHERE {filtered_column} IN ({placeholders})",
            values,
        )
    )


def _delete_where_in(
    connection: sqlite3.Connection,
    table: str,
    column_expression: str,
    values: Iterable[str],
) -> None:
    normalized = tuple(dict.fromkeys(str(value) for value in values))
    if not normalized:
        return
    placeholders = ",".join("?" for _ in normalized)
    connection.execute(
        f"DELETE FROM {table} WHERE {column_expression} IN ({placeholders})",
        normalized,
    )


def _empty_summary() -> dict[str, int]:
    return {
        "kept_profile_papers": 0,
        "removed_papers": 0,
        "removed_attempts": 0,
        "removed_tasks": 0,
    }


__all__ = ["cleanup_runtime_history"]
