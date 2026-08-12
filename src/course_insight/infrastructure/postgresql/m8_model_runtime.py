"""PostgreSQL persistence for append-only M8 IRT runtime artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.base import ContractModel
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionResult,
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTParameterSet,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool


_TContract = TypeVar("_TContract", bound=ContractModel)
_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M8 model-runtime integrity check failed"
_CONFLICT_ERROR = "PostgreSQL M8 model-runtime identity conflict"

_PARAMETER_COLUMNS = """
parameter_set_id, course_id, version, created_at,
payload, payload_checksum, schema_version
"""
_RUN_COLUMNS = """
run_id, course_id, model_version, generated_at,
payload, payload_checksum, schema_version
"""
_REVIEW_COLUMNS = """
decision_id, calibration_run_id, reviewed_at,
payload, payload_checksum, schema_version
"""
_ABILITY_COLUMNS = """
estimate_id, course_id, learner_id, parameter_set_id, estimated_at,
payload, payload_checksum, schema_version
"""
_SELECTION_COLUMNS = """
selection_id, course_id, learner_id, selected_at,
payload, payload_checksum, schema_version
"""


def insert_or_get_calibration_run(
    pool: PostgresPool,
    result: CalibrationRunResult,
    *,
    course_id: str,
) -> CalibrationRunResult:
    candidate = _isolated_contract(result, CalibrationRunResult)
    scope = _require_scope(course_id)
    try:
        with pool.connection() as connection:
            with connection.transaction():
                _insert_parameter_set(connection, candidate.parameter_set, scope)
                connection.execute(
                    """
                    INSERT INTO m8_irt_calibration_runs(
                        run_id, course_id, model_version, generated_at,
                        payload, payload_checksum, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (
                        candidate.run_id,
                        scope,
                        candidate.parameter_set.version,
                        candidate.generated_at,
                        Jsonb(candidate.to_dict()),
                        candidate.content_checksum(),
                        candidate.schema_version,
                    ),
                )
                row = connection.execute(
                    f"""
                    SELECT {_RUN_COLUMNS}
                    FROM m8_irt_calibration_runs
                    WHERE run_id = %s
                    """,
                    (candidate.run_id,),
                ).fetchone()
                stored = _calibration_from_row(row)
                if (
                    stored != candidate
                    or _required_text(row, "course_id") != scope
                    or _required_text(row, "model_version")
                    != candidate.parameter_set.version
                ):
                    raise PostgresOperationError(_CONFLICT_ERROR)
                return stored
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_calibration_run(
    pool: PostgresPool,
    run_id: str,
) -> CalibrationRunResult | None:
    try:
        with pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_RUN_COLUMNS}
                FROM m8_irt_calibration_runs
                WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
            return None if row is None else _calibration_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_calibration_run_course_id(pool: PostgresPool, run_id: str) -> str | None:
    return _scope_value(pool, "m8_irt_calibration_runs", "run_id", run_id)


def insert_or_get_parameter_set(
    pool: PostgresPool,
    parameter_set: IRTParameterSet,
    *,
    course_id: str,
) -> IRTParameterSet:
    candidate = _isolated_contract(parameter_set, IRTParameterSet)
    scope = _require_scope(course_id)
    try:
        with pool.connection() as connection:
            with connection.transaction():
                return _insert_parameter_set(connection, candidate, scope)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_parameter_set(
    pool: PostgresPool,
    parameter_set_id: str,
) -> IRTParameterSet | None:
    try:
        with pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_PARAMETER_COLUMNS}
                FROM m8_irt_parameter_sets
                WHERE parameter_set_id = %s
                """,
                (parameter_set_id,),
            ).fetchone()
            return None if row is None else _parameter_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_parameter_set_course_id(
    pool: PostgresPool,
    parameter_set_id: str,
) -> str | None:
    return _scope_value(
        pool,
        "m8_irt_parameter_sets",
        "parameter_set_id",
        parameter_set_id,
    )


def list_parameter_sets(
    pool: PostgresPool,
    *,
    course_id: str,
) -> list[IRTParameterSet]:
    scope = _require_scope(course_id)
    try:
        with pool.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_PARAMETER_COLUMNS}
                FROM m8_irt_parameter_sets
                WHERE course_id = %s
                ORDER BY created_at, parameter_set_id
                """,
                (scope,),
            ).fetchall()
            return [_parameter_from_row(row) for row in rows]
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def insert_or_get_calibration_review(
    pool: PostgresPool,
    decision: CalibrationReviewDecision,
    reviewed_parameter_set: IRTParameterSet,
    *,
    course_id: str,
) -> tuple[CalibrationReviewDecision, IRTParameterSet]:
    candidate_decision = _isolated_contract(decision, CalibrationReviewDecision)
    candidate_parameters = _isolated_contract(
        reviewed_parameter_set,
        IRTParameterSet,
    )
    scope = _require_scope(course_id)
    try:
        with pool.connection() as connection:
            with connection.transaction():
                run_scope = connection.execute(
                    """
                    SELECT course_id
                    FROM m8_irt_calibration_runs
                    WHERE run_id = %s
                    """,
                    (candidate_decision.calibration_run_id,),
                ).fetchone()
                if run_scope is None or _required_text(run_scope, "course_id") != scope:
                    raise PostgresOperationError(_CONFLICT_ERROR)
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
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        candidate_decision.decision_id,
                        candidate_decision.calibration_run_id,
                        candidate_decision.reviewed_at,
                        Jsonb(candidate_decision.to_dict()),
                        candidate_decision.content_checksum(),
                        candidate_decision.schema_version,
                    ),
                )
                row = connection.execute(
                    f"""
                    SELECT {_REVIEW_COLUMNS}
                    FROM m8_calibration_reviews
                    WHERE decision_id = %s OR calibration_run_id = %s
                    ORDER BY (decision_id = %s) DESC
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
                    raise PostgresOperationError(_CONFLICT_ERROR)
                return stored_decision, stored_parameters
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_calibration_review(
    pool: PostgresPool,
    calibration_run_id: str,
) -> CalibrationReviewDecision | None:
    try:
        with pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_REVIEW_COLUMNS}
                FROM m8_calibration_reviews
                WHERE calibration_run_id = %s
                """,
                (calibration_run_id,),
            ).fetchone()
            return None if row is None else _review_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def insert_or_get_ability_estimate(
    pool: PostgresPool,
    estimate: AbilityEstimate,
    *,
    course_id: str,
) -> AbilityEstimate:
    candidate = _isolated_contract(estimate, AbilityEstimate)
    scope = _require_scope(course_id)
    try:
        with pool.connection() as connection:
            with connection.transaction():
                parameter_row = connection.execute(
                    f"""
                    SELECT {_PARAMETER_COLUMNS}
                    FROM m8_irt_parameter_sets
                    WHERE parameter_set_id = %s
                    """,
                    (candidate.parameter_set_id,),
                ).fetchone()
                if (
                    parameter_row is None
                    or _required_text(parameter_row, "course_id") != scope
                    or _parameter_from_row(parameter_row).status != "approved"
                ):
                    raise PostgresOperationError(_CONFLICT_ERROR)
                connection.execute(
                    """
                    INSERT INTO m8_ability_estimates(
                        estimate_id, course_id, learner_id, parameter_set_id,
                        estimated_at, payload, payload_checksum, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (estimate_id) DO NOTHING
                    """,
                    (
                        candidate.estimate_id,
                        scope,
                        candidate.learner_id,
                        candidate.parameter_set_id,
                        candidate.estimated_at,
                        Jsonb(candidate.to_dict()),
                        candidate.content_checksum(),
                        candidate.schema_version,
                    ),
                )
                row = connection.execute(
                    f"""
                    SELECT {_ABILITY_COLUMNS}
                    FROM m8_ability_estimates
                    WHERE estimate_id = %s
                    """,
                    (candidate.estimate_id,),
                ).fetchone()
                stored = _ability_from_row(row)
                if stored != candidate or _required_text(row, "course_id") != scope:
                    raise PostgresOperationError(_CONFLICT_ERROR)
                return stored
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_ability_estimate(
    pool: PostgresPool,
    estimate_id: str,
) -> AbilityEstimate | None:
    try:
        with pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_ABILITY_COLUMNS}
                FROM m8_ability_estimates
                WHERE estimate_id = %s
                """,
                (estimate_id,),
            ).fetchone()
            return None if row is None else _ability_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def insert_or_get_adaptive_selection(
    pool: PostgresPool,
    selection: AdaptiveSelectionResult,
    *,
    course_id: str,
) -> AdaptiveSelectionResult:
    candidate = _isolated_contract(selection, AdaptiveSelectionResult)
    scope = _require_scope(course_id)
    if candidate.status == "empty" or candidate.ability_estimate is None:
        raise ValueError("M8 cannot persist an unconfigured adaptive selection")
    try:
        with pool.connection() as connection:
            with connection.transaction():
                parameter_scope = connection.execute(
                    """
                    SELECT course_id
                    FROM m8_irt_parameter_sets
                    WHERE parameter_set_id = %s
                    """,
                    (candidate.ability_estimate.parameter_set_id,),
                ).fetchone()
                if (
                    parameter_scope is None
                    or _required_text(parameter_scope, "course_id") != scope
                ):
                    raise PostgresOperationError(_CONFLICT_ERROR)
                connection.execute(
                    """
                    INSERT INTO m8_adaptive_selections(
                        selection_id, course_id, learner_id, selected_at,
                        payload, payload_checksum, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (selection_id) DO NOTHING
                    """,
                    (
                        candidate.selection_id,
                        scope,
                        candidate.learner_id,
                        candidate.selected_at,
                        Jsonb(candidate.to_dict()),
                        candidate.content_checksum(),
                        candidate.schema_version,
                    ),
                )
                row = connection.execute(
                    f"""
                    SELECT {_SELECTION_COLUMNS}
                    FROM m8_adaptive_selections
                    WHERE selection_id = %s
                    """,
                    (candidate.selection_id,),
                ).fetchone()
                stored = _selection_from_row(row)
                if stored != candidate or _required_text(row, "course_id") != scope:
                    raise PostgresOperationError(_CONFLICT_ERROR)
                return stored
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def get_adaptive_selection(
    pool: PostgresPool,
    selection_id: str,
) -> AdaptiveSelectionResult | None:
    try:
        with pool.connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_SELECTION_COLUMNS}
                FROM m8_adaptive_selections
                WHERE selection_id = %s
                """,
                (selection_id,),
            ).fetchone()
            return None if row is None else _selection_from_row(row)
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def _insert_parameter_set(
    connection: Any,
    candidate: IRTParameterSet,
    course_id: str,
) -> IRTParameterSet:
    connection.execute(
        """
        INSERT INTO m8_irt_parameter_sets(
            parameter_set_id, course_id, version, created_at,
            payload, payload_checksum, schema_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            candidate.parameter_set_id,
            course_id,
            candidate.version,
            candidate.created_at,
            Jsonb(candidate.to_dict()),
            candidate.content_checksum(),
            candidate.schema_version,
        ),
    )
    row = connection.execute(
        f"""
        SELECT {_PARAMETER_COLUMNS}
        FROM m8_irt_parameter_sets
        WHERE parameter_set_id = %s OR (course_id = %s AND version = %s)
        ORDER BY (parameter_set_id = %s) DESC
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
    if stored != candidate or _required_text(row, "course_id") != course_id:
        raise PostgresOperationError(_CONFLICT_ERROR)
    return stored


def _scope_value(
    pool: PostgresPool,
    table: str,
    identity_column: str,
    identity: str,
) -> str | None:
    allowed = {
        ("m8_irt_calibration_runs", "run_id"),
        ("m8_irt_parameter_sets", "parameter_set_id"),
    }
    if (table, identity_column) not in allowed:
        raise ValueError("unsupported M8 model scope lookup")
    try:
        with pool.connection() as connection:
            row = connection.execute(
                f"SELECT course_id FROM {table} WHERE {identity_column} = %s",
                (identity,),
            ).fetchone()
            return None if row is None else _required_text(row, "course_id")
    except PostgresError:
        raise
    except psycopg.Error:
        raise PostgresOperationError(_OPERATION_ERROR) from None


def _parameter_from_row(row: Mapping[str, Any] | None) -> IRTParameterSet:
    parameter_set = _contract_from_row(row, IRTParameterSet)
    if (
        parameter_set.parameter_set_id != _required_text(row, "parameter_set_id")
        or parameter_set.version != _required_text(row, "version")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return parameter_set


def _calibration_from_row(row: Mapping[str, Any] | None) -> CalibrationRunResult:
    result = _contract_from_row(row, CalibrationRunResult)
    if (
        result.run_id != _required_text(row, "run_id")
        or result.parameter_set.version != _required_text(row, "model_version")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return result


def _review_from_row(row: Mapping[str, Any] | None) -> CalibrationReviewDecision:
    decision = _contract_from_row(row, CalibrationReviewDecision)
    if (
        decision.decision_id != _required_text(row, "decision_id")
        or decision.calibration_run_id
        != _required_text(row, "calibration_run_id")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return decision


def _ability_from_row(row: Mapping[str, Any] | None) -> AbilityEstimate:
    estimate = _contract_from_row(row, AbilityEstimate)
    if (
        estimate.estimate_id != _required_text(row, "estimate_id")
        or estimate.learner_id != _required_text(row, "learner_id")
        or estimate.parameter_set_id != _required_text(row, "parameter_set_id")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return estimate


def _selection_from_row(
    row: Mapping[str, Any] | None,
) -> AdaptiveSelectionResult:
    selection = _contract_from_row(row, AdaptiveSelectionResult)
    if (
        selection.selection_id != _required_text(row, "selection_id")
        or selection.learner_id != _required_text(row, "learner_id")
    ):
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return selection


def _contract_from_row(
    row: Mapping[str, Any] | None,
    contract_type: type[_TContract],
) -> _TContract:
    if row is None:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    try:
        contract = contract_type.model_validate(row["payload"])
        if (
            contract.content_checksum() != _required_text(row, "payload_checksum")
            or contract.schema_version != _required_text(row, "schema_version")
        ):
            raise ValueError
        return contract
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _isolated_contract(value: _TContract, contract_type: type[_TContract]) -> _TContract:
    return contract_type.model_validate(value.model_dump(mode="python"))


def _required_text(row: Mapping[str, Any] | None, field: str) -> str:
    if row is None:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    value = row[field]
    if type(value) is not str or not value:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    return value


def _require_scope(course_id: str) -> str:
    if type(course_id) is not str or not course_id.strip():
        raise ValueError("M8 model course scope must not be blank")
    return course_id.strip()


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
