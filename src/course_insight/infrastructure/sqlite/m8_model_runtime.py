"""SQLite persistence for append-only M8 IRT runtime artifacts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC
from pathlib import Path
from typing import TypeVar

from course_insight.contracts.base import ContractModel
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionResult,
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTParameterSet,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite


_TContract = TypeVar("_TContract", bound=ContractModel)
_PARAMETER_CONFLICT = "M8 IRT parameter-set conflict"
_RUN_CONFLICT = "M8 IRT calibration-run conflict"
_REVIEW_CONFLICT = "M8 calibration review conflict"
_ABILITY_CONFLICT = "M8 ability-estimate conflict"
_SELECTION_CONFLICT = "M8 adaptive-selection conflict"
_INTEGRITY_ERROR = "M8 model-runtime persisted payload integrity error"


def insert_or_get_calibration_run(
    database_path: Path,
    result: CalibrationRunResult,
    *,
    course_id: str,
) -> CalibrationRunResult:
    candidate = _copy_contract(result, CalibrationRunResult)
    scope = _require_scope(course_id)
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _insert_parameter_set(connection, candidate.parameter_set, scope)
        connection.execute(
            """
            INSERT INTO m8_irt_calibration_runs(
                run_id, course_id, model_version, generated_at,
                payload, payload_checksum, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                candidate.run_id,
                scope,
                candidate.parameter_set.version,
                candidate.generated_at.astimezone(UTC).isoformat(),
                dumps_json(candidate.to_dict()),
                candidate.content_checksum(),
                candidate.schema_version,
            ),
        )
        row = connection.execute(
            """
            SELECT run_id, course_id, model_version, generated_at,
                   payload, payload_checksum, schema_version
            FROM m8_irt_calibration_runs
            WHERE run_id = ?
            """,
            (candidate.run_id,),
        ).fetchone()
        stored = _calibration_from_row(row)
        if (
            stored != candidate
            or str(row["course_id"]) != scope
            or str(row["model_version"]) != candidate.parameter_set.version
        ):
            raise RuntimeError(_RUN_CONFLICT)
        connection.execute("COMMIT")
        return stored.model_copy(deep=True)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_calibration_run(
    database_path: Path,
    run_id: str,
) -> CalibrationRunResult | None:
    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            """
            SELECT run_id, course_id, model_version, generated_at,
                   payload, payload_checksum, schema_version
            FROM m8_irt_calibration_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else _calibration_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def get_calibration_run_course_id(database_path: Path, run_id: str) -> str | None:
    return _scope_value(database_path, "m8_irt_calibration_runs", "run_id", run_id)


def insert_or_get_parameter_set(
    database_path: Path,
    parameter_set: IRTParameterSet,
    *,
    course_id: str,
) -> IRTParameterSet:
    candidate = _copy_contract(parameter_set, IRTParameterSet)
    scope = _require_scope(course_id)
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stored = _insert_parameter_set(connection, candidate, scope)
        connection.execute("COMMIT")
        return stored.model_copy(deep=True)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_parameter_set(
    database_path: Path,
    parameter_set_id: str,
) -> IRTParameterSet | None:
    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            """
            SELECT parameter_set_id, course_id, version, created_at,
                   payload, payload_checksum, schema_version
            FROM m8_irt_parameter_sets
            WHERE parameter_set_id = ?
            """,
            (parameter_set_id,),
        ).fetchone()
        return None if row is None else _parameter_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def get_parameter_set_course_id(
    database_path: Path,
    parameter_set_id: str,
) -> str | None:
    return _scope_value(
        database_path,
        "m8_irt_parameter_sets",
        "parameter_set_id",
        parameter_set_id,
    )


def list_parameter_sets(
    database_path: Path,
    *,
    course_id: str,
) -> list[IRTParameterSet]:
    scope = _require_scope(course_id)
    connection = connect_sqlite(database_path)
    try:
        rows = connection.execute(
            """
            SELECT parameter_set_id, course_id, version, created_at,
                   payload, payload_checksum, schema_version
            FROM m8_irt_parameter_sets
            WHERE course_id = ?
            ORDER BY created_at, parameter_set_id
            """,
            (scope,),
        ).fetchall()
        return [_parameter_from_row(row).model_copy(deep=True) for row in rows]
    finally:
        connection.close()


def insert_or_get_calibration_review(
    database_path: Path,
    decision: CalibrationReviewDecision,
    reviewed_parameter_set: IRTParameterSet,
    *,
    course_id: str,
) -> tuple[CalibrationReviewDecision, IRTParameterSet]:
    candidate_decision = _copy_contract(decision, CalibrationReviewDecision)
    candidate_parameters = _copy_contract(reviewed_parameter_set, IRTParameterSet)
    scope = _require_scope(course_id)
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        run_row = connection.execute(
            """
            SELECT course_id
            FROM m8_irt_calibration_runs
            WHERE run_id = ?
            """,
            (candidate_decision.calibration_run_id,),
        ).fetchone()
        if run_row is None or str(run_row["course_id"]) != scope:
            raise RuntimeError(_REVIEW_CONFLICT)
        stored_parameters = _insert_parameter_set(
            connection,
            candidate_parameters,
            scope,
        )
        connection.execute(
            """
            INSERT INTO m8_calibration_reviews(
                decision_id, calibration_run_id, reviewed_at,
                payload, payload_checksum, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                candidate_decision.decision_id,
                candidate_decision.calibration_run_id,
                candidate_decision.reviewed_at.astimezone(UTC).isoformat(),
                dumps_json(candidate_decision.to_dict()),
                candidate_decision.content_checksum(),
                candidate_decision.schema_version,
            ),
        )
        row = connection.execute(
            """
            SELECT decision_id, calibration_run_id, reviewed_at,
                   payload, payload_checksum, schema_version
            FROM m8_calibration_reviews
            WHERE decision_id = ? OR calibration_run_id = ?
            ORDER BY CASE WHEN decision_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (
                candidate_decision.decision_id,
                candidate_decision.calibration_run_id,
                candidate_decision.decision_id,
            ),
        ).fetchone()
        stored_decision = _review_from_row(row)
        if stored_decision != candidate_decision:
            raise RuntimeError(_REVIEW_CONFLICT)
        connection.execute("COMMIT")
        return (
            stored_decision.model_copy(deep=True),
            stored_parameters.model_copy(deep=True),
        )
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_calibration_review(
    database_path: Path,
    calibration_run_id: str,
) -> CalibrationReviewDecision | None:
    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            """
            SELECT decision_id, calibration_run_id, reviewed_at,
                   payload, payload_checksum, schema_version
            FROM m8_calibration_reviews
            WHERE calibration_run_id = ?
            """,
            (calibration_run_id,),
        ).fetchone()
        return None if row is None else _review_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def insert_or_get_ability_estimate(
    database_path: Path,
    estimate: AbilityEstimate,
    *,
    course_id: str,
) -> AbilityEstimate:
    candidate = _copy_contract(estimate, AbilityEstimate)
    scope = _require_scope(course_id)
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parameter_row = connection.execute(
            """
            SELECT parameter_set_id, course_id, version, created_at,
                   payload, payload_checksum, schema_version
            FROM m8_irt_parameter_sets
            WHERE parameter_set_id = ?
            """,
            (candidate.parameter_set_id,),
        ).fetchone()
        if (
            parameter_row is None
            or str(parameter_row["course_id"]) != scope
            or _parameter_from_row(parameter_row).status != "approved"
        ):
            raise RuntimeError(_ABILITY_CONFLICT)
        connection.execute(
            """
            INSERT INTO m8_ability_estimates(
                estimate_id, course_id, learner_id, parameter_set_id,
                estimated_at, payload, payload_checksum, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                candidate.estimate_id,
                scope,
                candidate.learner_id,
                candidate.parameter_set_id,
                candidate.estimated_at.astimezone(UTC).isoformat(),
                dumps_json(candidate.to_dict()),
                candidate.content_checksum(),
                candidate.schema_version,
            ),
        )
        row = connection.execute(
            """
            SELECT estimate_id, course_id, learner_id, parameter_set_id,
                   estimated_at, payload, payload_checksum, schema_version
            FROM m8_ability_estimates
            WHERE estimate_id = ?
            """,
            (candidate.estimate_id,),
        ).fetchone()
        stored = _ability_from_row(row)
        if stored != candidate or str(row["course_id"]) != scope:
            raise RuntimeError(_ABILITY_CONFLICT)
        connection.execute("COMMIT")
        return stored.model_copy(deep=True)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_ability_estimate(
    database_path: Path,
    estimate_id: str,
) -> AbilityEstimate | None:
    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            """
            SELECT estimate_id, course_id, learner_id, parameter_set_id,
                   estimated_at, payload, payload_checksum, schema_version
            FROM m8_ability_estimates
            WHERE estimate_id = ?
            """,
            (estimate_id,),
        ).fetchone()
        return None if row is None else _ability_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def insert_or_get_adaptive_selection(
    database_path: Path,
    selection: AdaptiveSelectionResult,
    *,
    course_id: str,
) -> AdaptiveSelectionResult:
    candidate = _copy_contract(selection, AdaptiveSelectionResult)
    scope = _require_scope(course_id)
    if candidate.status == "empty" or candidate.ability_estimate is None:
        raise ValueError("M8 cannot persist an unconfigured adaptive selection")
    connection = connect_sqlite(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        parameter_scope = connection.execute(
            """
            SELECT course_id
            FROM m8_irt_parameter_sets
            WHERE parameter_set_id = ?
            """,
            (candidate.ability_estimate.parameter_set_id,),
        ).fetchone()
        if parameter_scope is None or str(parameter_scope["course_id"]) != scope:
            raise RuntimeError(_SELECTION_CONFLICT)
        connection.execute(
            """
            INSERT INTO m8_adaptive_selections(
                selection_id, course_id, learner_id, selected_at,
                payload, payload_checksum, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                candidate.selection_id,
                scope,
                candidate.learner_id,
                candidate.selected_at.astimezone(UTC).isoformat(),
                dumps_json(candidate.to_dict()),
                candidate.content_checksum(),
                candidate.schema_version,
            ),
        )
        row = connection.execute(
            """
            SELECT selection_id, course_id, learner_id, selected_at,
                   payload, payload_checksum, schema_version
            FROM m8_adaptive_selections
            WHERE selection_id = ?
            """,
            (candidate.selection_id,),
        ).fetchone()
        stored = _selection_from_row(row)
        if stored != candidate or str(row["course_id"]) != scope:
            raise RuntimeError(_SELECTION_CONFLICT)
        connection.execute("COMMIT")
        return stored.model_copy(deep=True)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_adaptive_selection(
    database_path: Path,
    selection_id: str,
) -> AdaptiveSelectionResult | None:
    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            """
            SELECT selection_id, course_id, learner_id, selected_at,
                   payload, payload_checksum, schema_version
            FROM m8_adaptive_selections
            WHERE selection_id = ?
            """,
            (selection_id,),
        ).fetchone()
        return None if row is None else _selection_from_row(row).model_copy(deep=True)
    finally:
        connection.close()


def _insert_parameter_set(
    connection: sqlite3.Connection,
    candidate: IRTParameterSet,
    course_id: str,
) -> IRTParameterSet:
    connection.execute(
        """
        INSERT INTO m8_irt_parameter_sets(
            parameter_set_id, course_id, version, created_at,
            payload, payload_checksum, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            candidate.parameter_set_id,
            course_id,
            candidate.version,
            candidate.created_at.astimezone(UTC).isoformat(),
            dumps_json(candidate.to_dict()),
            candidate.content_checksum(),
            candidate.schema_version,
        ),
    )
    row = connection.execute(
        """
        SELECT parameter_set_id, course_id, version, created_at,
               payload, payload_checksum, schema_version
        FROM m8_irt_parameter_sets
        WHERE parameter_set_id = ? OR (course_id = ? AND version = ?)
        ORDER BY CASE WHEN parameter_set_id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (
            candidate.parameter_set_id,
            course_id,
            candidate.version,
            candidate.parameter_set_id,
        ),
    ).fetchone()
    stored = _parameter_from_row(row)
    if stored != candidate or str(row["course_id"]) != course_id:
        raise RuntimeError(_PARAMETER_CONFLICT)
    return stored


def _scope_value(
    database_path: Path,
    table: str,
    identity_column: str,
    identity: str,
) -> str | None:
    allowed = {
        ("m8_irt_calibration_runs", "run_id"),
        ("m8_irt_parameter_sets", "parameter_set_id"),
    }
    if (table, identity_column) not in allowed:
        raise ValueError("unsupported M8 scope lookup")
    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            f"SELECT course_id FROM {table} WHERE {identity_column} = ?",
            (identity,),
        ).fetchone()
        return None if row is None else str(row["course_id"])
    finally:
        connection.close()


def _parameter_from_row(row: sqlite3.Row | None) -> IRTParameterSet:
    parameter_set = _contract_from_row(row, IRTParameterSet)
    if (
        parameter_set.parameter_set_id != str(row["parameter_set_id"])
        or parameter_set.version != str(row["version"])
    ):
        raise RuntimeError(_INTEGRITY_ERROR)
    return parameter_set


def _calibration_from_row(row: sqlite3.Row | None) -> CalibrationRunResult:
    result = _contract_from_row(row, CalibrationRunResult)
    if (
        result.run_id != str(row["run_id"])
        or result.parameter_set.version != str(row["model_version"])
    ):
        raise RuntimeError(_INTEGRITY_ERROR)
    return result


def _review_from_row(row: sqlite3.Row | None) -> CalibrationReviewDecision:
    decision = _contract_from_row(row, CalibrationReviewDecision)
    if (
        decision.decision_id != str(row["decision_id"])
        or decision.calibration_run_id != str(row["calibration_run_id"])
    ):
        raise RuntimeError(_INTEGRITY_ERROR)
    return decision


def _ability_from_row(row: sqlite3.Row | None) -> AbilityEstimate:
    estimate = _contract_from_row(row, AbilityEstimate)
    if (
        estimate.estimate_id != str(row["estimate_id"])
        or estimate.learner_id != str(row["learner_id"])
        or estimate.parameter_set_id != str(row["parameter_set_id"])
    ):
        raise RuntimeError(_INTEGRITY_ERROR)
    return estimate


def _selection_from_row(row: sqlite3.Row | None) -> AdaptiveSelectionResult:
    selection = _contract_from_row(row, AdaptiveSelectionResult)
    if (
        selection.selection_id != str(row["selection_id"])
        or selection.learner_id != str(row["learner_id"])
    ):
        raise RuntimeError(_INTEGRITY_ERROR)
    return selection


def _contract_from_row(
    row: sqlite3.Row | None,
    contract_type: type[_TContract],
) -> _TContract:
    if row is None:
        raise RuntimeError(_INTEGRITY_ERROR)
    try:
        payload = json.loads(str(row["payload"]))
        contract = contract_type.model_validate(payload)
        if (
            contract.content_checksum() != str(row["payload_checksum"])
            or contract.schema_version != str(row["schema_version"])
        ):
            raise ValueError
        return contract
    except Exception as error:
        raise RuntimeError(_INTEGRITY_ERROR) from error


def _copy_contract(value: _TContract, contract_type: type[_TContract]) -> _TContract:
    return contract_type.model_validate(value.model_dump(mode="python"))


def _require_scope(course_id: str) -> str:
    scope = course_id.strip()
    if not scope:
        raise ValueError("M8 model course scope must not be blank")
    return scope


__all__ = [
    "get_adaptive_selection",
    "get_ability_estimate",
    "get_calibration_review",
    "get_calibration_run",
    "get_calibration_run_course_id",
    "get_parameter_set",
    "get_parameter_set_course_id",
    "insert_or_get_ability_estimate",
    "insert_or_get_adaptive_selection",
    "insert_or_get_calibration_review",
    "insert_or_get_calibration_run",
    "insert_or_get_parameter_set",
    "list_parameter_sets",
]
