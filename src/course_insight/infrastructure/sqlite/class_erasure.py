"""Exact class-scope physical erasure for the shared SQLite runtime."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

# This is an allow-list on purpose.  A Django app label also begins with
# ``m0_``; no class cleaner may discover tables by prefix and accidentally
# delete Django-owned identities or authorization records.
_RUNTIME_TABLES = (
    "m0_event_outbox",
    "m0_learning_events",
    "m0_assessment_runs",
    "m9_model_invocation_audits",
    "m9_teacher_reviews",
    "m9_teacher_analytics",
    "m8_score_audits",
    "m8_scoring_results",
    "m8_frozen_assessment_records",
    "m8_assessment_papers",
    "m8_adaptive_selections",
    "m8_ability_estimates",
    "m8_calibration_reviews",
    "m8_irt_calibration_runs",
    "m8_irt_parameter_sets",
    "m7_model_invocation_audits",
    "m7_student_feedback",
    "m6_policy_observations",
    "m6_policy_rewards",
    "m6_tutoring_decisions",
    "m6_policy_executions",
    "m6_session_states",
    "m5_learning_observation_audits",
    "m5_learning_observations",
    "m5_state_updates",
    "m5_knowledge_traces",
    "m5_dina_models",
    "m5_bkt_models",
    "m5_class_states",
    "m5_learner_states",
    "m4_intent_decisions",
    "m4_task_plans",
)
_ARTIFACT_TABLES = (
    "m1_course_packages",
    "m2_evidence_indexes",
    "m3_knowledge_bundles",
    "s1_s6_artifacts",
)
_JSON_COLUMN_NAMES = frozenset(
    {
        "payload",
        "record",
        "result_payload",
        "evidence_identity",
        "shadow_json",
        "learner_ids",
    }
)


@dataclass(frozen=True, slots=True)
class ClassErasureResult:
    """Count rows erased from each runtime table and frozen submissions."""

    table_counts: tuple[tuple[str, int], ...]
    frozen_attempt_ids: tuple[str, ...]

    @property
    def total_deleted(self) -> int:
        return sum(count for _, count in self.table_counts)


def purge_sqlite_class_scope(
    connection: sqlite3.Connection,
    *,
    course_id: str,
    class_id: str,
) -> ClassErasureResult:
    """Erase runtime rows that belong to one exact ``(course, class)`` pair.

    The caller owns the transaction.  Every value is bound as a query
    parameter and every table name comes from a static allow-list.
    """

    _validate_scope(course_id, class_id)
    existing = _existing_tables(connection)
    counts: dict[str, int] = {}

    frozen_attempt_ids = _select_scoped_values(
        connection,
        existing,
        "m0_assessment_runs",
        "attempt_id",
        course_id,
        class_id,
    )
    paper_ids = _select_scoped_values(
        connection,
        existing,
        "m8_assessment_papers",
        "paper_id",
        course_id,
        class_id,
    )
    paper_ids = tuple(
        sorted(
            set(paper_ids)
            | set(
                _select_scoped_values(
                    connection,
                    existing,
                    "m0_assessment_runs",
                    "paper_id",
                    course_id,
                    class_id,
                )
            )
        )
    )
    task_ids = tuple(
        sorted(
            set(
                _select_scoped_values(
                    connection,
                    existing,
                    "m8_assessment_papers",
                    "task_id",
                    course_id,
                    class_id,
                )
            )
            | set(
                _select_scoped_values(
                    connection,
                    existing,
                    "m4_task_plans",
                    "task_id",
                    course_id,
                    class_id,
                )
            )
            | set(
                _select_scoped_values(
                    connection,
                    existing,
                    "m0_assessment_runs",
                    "task_id",
                    course_id,
                    class_id,
                )
            )
        )
    )
    report_ids = _select_scoped_values(
        connection,
        existing,
        "m9_teacher_analytics",
        "report_id",
        course_id,
        class_id,
    )
    session_ids = _select_scoped_values(
        connection,
        existing,
        "m6_session_states",
        "session_id",
        course_id,
        class_id,
    )
    session_ids = tuple(
        sorted(
            set(session_ids)
            | set(
                _select_scoped_values(
                    connection,
                    existing,
                    "m0_assessment_runs",
                    "session_id",
                    course_id,
                    class_id,
                )
            )
        )
    )
    observation_ids = _select_scoped_values(
        connection,
        existing,
        "m5_learning_observations",
        "observation_id",
        course_id,
        class_id,
    )
    artifact_references = _collect_scoped_artifact_references(
        connection,
        existing,
        course_id=course_id,
        class_id=class_id,
    )

    # Delete relationship-owned leaves before their exact scoped parents.
    _add_count(
        counts,
        "m9_model_invocation_audits",
        _delete_in_values(
            connection,
            existing,
            "m9_model_invocation_audits",
            "source_report_id",
            report_ids,
        ),
    )
    _add_count(
        counts,
        "m7_model_invocation_audits",
        _delete_in_values(
            connection,
            existing,
            "m7_model_invocation_audits",
            "scoring_task_id",
            task_ids,
        ),
    )
    _add_count(
        counts,
        "m7_student_feedback",
        _delete_in_values(
            connection,
            existing,
            "m7_student_feedback",
            "task_id",
            task_ids,
        ),
    )
    _add_count(
        counts,
        "m8_scoring_results",
        _delete_in_values(
            connection,
            existing,
            "m8_scoring_results",
            "paper_id",
            paper_ids,
        ),
    )
    _add_count(
        counts,
        "m8_frozen_assessment_records",
        _delete_in_values(
            connection,
            existing,
            "m8_frozen_assessment_records",
            "paper_id",
            paper_ids,
        ),
    )
    _add_count(
        counts,
        "m5_learning_observation_audits",
        _delete_in_values(
            connection,
            existing,
            "m5_learning_observation_audits",
            "observation_id",
            observation_ids,
        ),
    )
    _purge_m6_dependants(connection, existing, session_ids, counts)

    for table in _RUNTIME_TABLES:
        _add_count(
            counts,
            table,
            _delete_scoped_rows(
                connection,
                existing,
                table,
                course_id,
                class_id,
            ),
        )

    _purge_unreferenced_artifacts(
        connection,
        existing,
        references=artifact_references,
        counts=counts,
    )
    return ClassErasureResult(
        table_counts=tuple(
            (table, count) for table, count in counts.items() if count > 0
        ),
        frozen_attempt_ids=tuple(sorted(set(frozen_attempt_ids))),
    )


def _validate_scope(course_id: str, class_id: str) -> None:
    if not isinstance(course_id, str) or not _SCOPE.fullmatch(course_id):
        raise ValueError("course erasure scope is invalid")
    if not isinstance(class_id, str) or not _SCOPE.fullmatch(class_id):
        raise ValueError("class erasure scope is invalid")


def _existing_tables(connection: sqlite3.Connection) -> frozenset[str]:
    allowed = frozenset((*_RUNTIME_TABLES, *_ARTIFACT_TABLES))
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        if str(row[0]) in allowed
    )


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    _require_known_table(table)
    return frozenset(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if _IDENTIFIER.fullmatch(str(row[1]))
    )


def _scope_clause(
    connection: sqlite3.Connection,
    table: str,
    *,
    course_id: str,
    class_id: str,
) -> tuple[str, tuple[str, ...]] | None:
    columns = _columns(connection, table)
    if {"course_id", "class_id"}.issubset(columns):
        return '"course_id" = ? AND "class_id" = ?', (course_id, class_id)
    json_columns = tuple(
        column for column in columns if column in _JSON_COLUMN_NAMES
    )
    if not json_columns:
        return None
    conditions: list[str] = []
    parameters: list[str] = []
    for column in json_columns:
        quoted = f'"{column}"'
        conditions.append(
            "(json_valid(" + quoted + ") AND "
            "EXISTS (SELECT 1 FROM json_tree(" + quoted
            + ") WHERE key = 'course_id' AND atom = ?) AND "
            "EXISTS (SELECT 1 FROM json_tree(" + quoted
            + ") WHERE key = 'class_id' AND atom = ?))"
        )
        parameters.extend((course_id, class_id))
    return " OR ".join(conditions), tuple(parameters)


def _select_scoped_values(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    table: str,
    column: str,
    course_id: str,
    class_id: str,
) -> tuple[str, ...]:
    if table not in existing or column not in _columns(connection, table):
        return ()
    scoped = _scope_clause(
        connection,
        table,
        course_id=course_id,
        class_id=class_id,
    )
    if scoped is None:
        return ()
    where, parameters = scoped
    rows = connection.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" '
        f'WHERE ({where}) AND "{column}" IS NOT NULL',
        parameters,
    ).fetchall()
    return tuple(str(row[0]) for row in rows if row[0] is not None)


def _delete_scoped_rows(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    table: str,
    course_id: str,
    class_id: str,
) -> int:
    if table not in existing:
        return 0
    scoped = _scope_clause(
        connection,
        table,
        course_id=course_id,
        class_id=class_id,
    )
    if scoped is None:
        return 0
    where, parameters = scoped
    cursor = connection.execute(
        f'DELETE FROM "{table}" WHERE {where}',
        parameters,
    )
    return max(cursor.rowcount, 0)


def _delete_in_values(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    table: str,
    column: str,
    values: tuple[str, ...],
) -> int:
    if (
        table not in existing
        or column not in _columns(connection, table)
        or not values
    ):
        return 0
    _require_known_table(table)
    deleted = 0
    for start in range(0, len(values), 900):
        batch = values[start : start + 900]
        marks = ", ".join("?" for _ in batch)
        cursor = connection.execute(
            f'DELETE FROM "{table}" WHERE "{column}" IN ({marks})',
            batch,
        )
        deleted += max(cursor.rowcount, 0)
    return deleted


def _purge_m6_dependants(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    session_ids: tuple[str, ...],
    counts: dict[str, int],
) -> None:
    decision_ids = _select_by_values(
        connection,
        existing,
        "m6_tutoring_decisions",
        "decision_id",
        "session_id",
        session_ids,
    )
    if decision_ids:
        fingerprints = _select_by_values(
            connection,
            existing,
            "m6_policy_observations",
            "request_fingerprint",
            "decision_id",
            decision_ids,
        )
        execution_fingerprints = _select_by_values(
            connection,
            existing,
            "m6_policy_observations",
            "policy_execution_fingerprint",
            "decision_id",
            decision_ids,
        )
        _add_count(
            counts,
            "m6_policy_observations",
            _delete_in_values(
                connection,
                existing,
                "m6_policy_observations",
                "decision_id",
                decision_ids,
            ),
        )
        _add_count(
            counts,
            "m6_policy_rewards",
            _delete_in_values(
                connection,
                existing,
                "m6_policy_rewards",
                "policy_execution_fingerprint",
                execution_fingerprints,
            ),
        )
        _add_count(
            counts,
            "m6_tutoring_decisions",
            _delete_in_values(
                connection,
                existing,
                "m6_tutoring_decisions",
                "decision_id",
                decision_ids,
            ),
        )
        _add_count(
            counts,
            "m6_policy_executions",
            _delete_in_values(
                connection,
                existing,
                "m6_policy_executions",
                "request_fingerprint",
                fingerprints,
            ),
        )
    _add_count(
        counts,
        "m6_session_states",
        _delete_in_values(
            connection,
            existing,
            "m6_session_states",
            "session_id",
            session_ids,
        ),
    )


def _select_by_values(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    table: str,
    result_column: str,
    match_column: str,
    values: tuple[str, ...],
) -> tuple[str, ...]:
    if (
        table not in existing
        or result_column not in _columns(connection, table)
        or match_column not in _columns(connection, table)
        or not values
    ):
        return ()
    result: set[str] = set()
    for start in range(0, len(values), 900):
        batch = values[start : start + 900]
        marks = ", ".join("?" for _ in batch)
        rows = connection.execute(
            f'SELECT DISTINCT "{result_column}" FROM "{table}" '
            f'WHERE "{match_column}" IN ({marks})',
            batch,
        ).fetchall()
        result.update(str(row[0]) for row in rows if row[0] is not None)
    return tuple(sorted(result))


def _purge_unreferenced_artifacts(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    *,
    references: tuple[
        tuple[str, str, str, str, str, str, str, str, str], ...
    ],
    counts: dict[str, int],
) -> None:
    """Remove only M1--M3 artifacts no remaining class run can reference."""

    if "m0_assessment_runs" not in existing:
        return
    run_columns = _columns(connection, "m0_assessment_runs")
    for (
        table,
        run_id_column,
        run_version_column,
        table_id_column,
        table_version_column,
        module,
        object_type,
        artifact_id,
        artifact_version,
    ) in references:
        if (
            table not in existing
            or run_id_column not in run_columns
            or run_version_column not in run_columns
            or {table_id_column, table_version_column}
            - _columns(connection, table)
        ):
            continue
        # At this point scoped assessment runs are already gone.  An artifact
        # is deletable only when no surviving run still names its exact pair.
        remaining = connection.execute(
            f'SELECT 1 FROM "m0_assessment_runs" '
            f'WHERE "{run_id_column}" = ? '
            f'AND "{run_version_column}" = ? LIMIT 1',
            (artifact_id, artifact_version),
        ).fetchone()
        if remaining is not None:
            continue
        _add_count(
            counts,
            table,
            _delete_exact_artifact(
                connection,
                table,
                table_id_column,
                table_version_column,
                artifact_id,
                artifact_version,
            ),
        )
        _add_count(
            counts,
            "s1_s6_artifacts",
            _delete_s1_s6_artifact(
                connection,
                existing,
                module,
                object_type,
                artifact_id,
                artifact_version,
            ),
        )


def _collect_scoped_artifact_references(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    *,
    course_id: str,
    class_id: str,
) -> tuple[tuple[str, str, str, str, str, str, str, str, str], ...]:
    """Capture only artifacts named by the class before its runs disappear."""

    if "m0_assessment_runs" not in existing:
        return ()
    specs = (
        (
            "m2_evidence_indexes",
            "evidence_index_id",
            "evidence_index_version",
            "index_id",
            "index_version",
            "m2",
            "evidence_index",
        ),
        (
            "m3_knowledge_bundles",
            "knowledge_bundle_id",
            "knowledge_bundle_version",
            "knowledge_bundle_id",
            "bundle_version",
            "m3",
            "knowledge_bundle",
        ),
    )
    run_columns = _columns(connection, "m0_assessment_runs")
    result: list[tuple[str, str, str, str, str, str, str, str, str]] = []
    for spec in specs:
        table, run_id_column, run_version_column, *_rest = spec
        if (
            table not in existing
            or run_id_column not in run_columns
            or run_version_column not in run_columns
        ):
            continue
        rows = connection.execute(
            f'SELECT DISTINCT "{run_id_column}", "{run_version_column}" '
            'FROM "m0_assessment_runs" '
            'WHERE "course_id" = ? AND "class_id" = ? '
            f'AND "{run_id_column}" IS NOT NULL '
            f'AND "{run_version_column}" IS NOT NULL',
            (course_id, class_id),
        ).fetchall()
        for row in rows:
            result.append(
                (
                    *spec,
                    str(row[0]),
                    str(row[1]),
                )
            )
    return tuple(result)


def _delete_exact_artifact(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    version_column: str,
    artifact_id: str,
    artifact_version: str,
) -> int:
    cursor = connection.execute(
        f'DELETE FROM "{table}" WHERE "{id_column}" = ? '
        f'AND "{version_column}" = ?',
        (artifact_id, artifact_version),
    )
    return max(cursor.rowcount, 0)


def _delete_s1_s6_artifact(
    connection: sqlite3.Connection,
    existing: frozenset[str],
    module: str,
    object_type: str,
    artifact_id: str,
    artifact_version: str,
) -> int:
    if "s1_s6_artifacts" not in existing:
        return 0
    cursor = connection.execute(
        """
        DELETE FROM s1_s6_artifacts
        WHERE module = ? AND object_type = ?
          AND object_id = ? AND object_version = ?
        """,
        (module, object_type, artifact_id, artifact_version),
    )
    return max(cursor.rowcount, 0)


def _add_count(counts: dict[str, int], table: str, deleted: int) -> None:
    if deleted > 0:
        counts[table] = counts.get(table, 0) + deleted


def _require_known_table(table: str) -> None:
    if table not in {*_RUNTIME_TABLES, *_ARTIFACT_TABLES}:
        raise ValueError("unsafe SQLite table identifier")


__all__ = ["ClassErasureResult", "purge_sqlite_class_scope"]
