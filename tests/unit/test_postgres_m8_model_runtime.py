"""PostgreSQL adapter tests for persisted M8 model runtime artifacts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from course_insight.contracts.learning_models import (
    AbilityEstimate,
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTItemParameters,
    IRTParameterSet,
)
from course_insight.infrastructure.postgresql.base import PostgresOperationError
from course_insight.infrastructure.postgresql.m8_repository import (
    PostgresM8Repository,
)
from tests.unit._postgres_repository_fakes import FakeConnection, FakePool


NOW = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)


def _shadow() -> IRTParameterSet:
    return IRTParameterSet(
        parameter_set_id="irt_shadow_pg",
        model_type="2PL",
        version="irt-pg-v1",
        item_parameters=[
            IRTItemParameters(
                item_id="item_1",
                item_version="1.0.0",
                discrimination=1.2,
                difficulty=0.1,
                guessing=0.0,
                sample_size=240,
            )
        ],
        sample_size=240,
        status="shadow",
        created_at=NOW,
    )


def _run() -> CalibrationRunResult:
    return CalibrationRunResult(
        run_id="calibration_pg",
        parameter_set=_shadow(),
        converged=True,
        metrics={"log_likelihood": -100.0},
        status="shadow",
        failure_code=None,
        generated_at=NOW,
    )


def _approved() -> IRTParameterSet:
    return _shadow().model_copy(
        update={
            "parameter_set_id": "irt_approved_pg",
            "version": "irt-pg-v1-approved",
            "status": "approved",
            "created_at": NOW + timedelta(minutes=1),
        }
    )


def _decision() -> CalibrationReviewDecision:
    return CalibrationReviewDecision(
        decision_id="decision_pg",
        calibration_run_id="calibration_pg",
        reviewer_id="teacher_1",
        decision="approve",
        target_parameter_version="irt-pg-v1",
        reason="quality passed",
        reviewed_at=NOW + timedelta(minutes=1),
    )


def _ability() -> AbilityEstimate:
    return AbilityEstimate(
        estimate_id="ability_pg",
        learner_id="learner_1",
        parameter_set_id="irt_approved_pg",
        theta=0.4,
        standard_error=0.7,
        status="estimated",
        estimated_at=NOW + timedelta(minutes=2),
    )


def _row(contract: Any, **columns: Any) -> dict[str, Any]:
    return {
        **columns,
        "payload": contract.to_dict(),
        "payload_checksum": contract.content_checksum(),
        "schema_version": contract.schema_version,
    }


def _model_responder(
    *,
    run: CalibrationRunResult,
    parameter_sets: list[IRTParameterSet],
    decision: CalibrationReviewDecision | None = None,
    ability: AbilityEstimate | None = None,
):
    by_parameter_id = {item.parameter_set_id: item for item in parameter_sets}

    def respond(statement: str, parameters: tuple[Any, ...]):
        normalized = " ".join(statement.lower().split())
        if "from m8_irt_calibration_runs" in normalized:
            if normalized.startswith("select course_id"):
                return {"course_id": "course_1"}
            return _row(
                run,
                run_id=run.run_id,
                course_id="course_1",
                model_version=run.parameter_set.version,
                generated_at=run.generated_at,
            )
        if "from m8_irt_parameter_sets" in normalized:
            if normalized.startswith("select course_id"):
                return {"course_id": "course_1"}
            if "order by created_at" in normalized:
                return [
                    _row(
                        item,
                        parameter_set_id=item.parameter_set_id,
                        course_id="course_1",
                        version=item.version,
                        created_at=item.created_at,
                    )
                    for item in parameter_sets
                ]
            requested = next(
                (
                    str(value)
                    for value in parameters
                    if str(value) in by_parameter_id
                ),
                parameter_sets[-1].parameter_set_id,
            )
            item = by_parameter_id[requested]
            return _row(
                item,
                parameter_set_id=item.parameter_set_id,
                course_id="course_1",
                version=item.version,
                created_at=item.created_at,
            )
        if "from m8_calibration_reviews" in normalized:
            if decision is None:
                return None
            return _row(
                decision,
                decision_id=decision.decision_id,
                calibration_run_id=decision.calibration_run_id,
                reviewed_at=decision.reviewed_at,
            )
        if "from m8_ability_estimates" in normalized:
            if ability is None:
                return None
            return _row(
                ability,
                estimate_id=ability.estimate_id,
                course_id="course_1",
                learner_id=ability.learner_id,
                parameter_set_id=ability.parameter_set_id,
                estimated_at=ability.estimated_at,
            )
        return None

    return respond


def test_postgres_persists_and_recovers_full_irt_history() -> None:
    """Catch PostgreSQL lagging behind the SQLite model lifecycle."""

    run = _run()
    approved = _approved()
    decision = _decision()
    ability = _ability()
    connection = FakeConnection(
        _model_responder(
            run=run,
            parameter_sets=[run.parameter_set, approved],
            decision=decision,
            ability=ability,
        )
    )
    repository = PostgresM8Repository(FakePool(connection))

    assert repository.insert_or_get_calibration_run(
        run,
        course_id="course_1",
    ) == run
    assert repository.get_calibration_run(run.run_id) == run
    assert repository.get_calibration_run_course_id(run.run_id) == "course_1"
    assert repository.insert_or_get_parameter_set(
        approved,
        course_id="course_1",
    ) == approved
    assert repository.get_parameter_set(approved.parameter_set_id) == approved
    assert repository.get_parameter_set_course_id(
        approved.parameter_set_id
    ) == "course_1"
    assert repository.list_parameter_sets(course_id="course_1") == [
        run.parameter_set,
        approved,
    ]
    assert repository.insert_or_get_calibration_review(
        decision,
        approved,
        course_id="course_1",
    ) == (decision, approved)
    assert repository.get_calibration_review(run.run_id) == decision
    assert repository.insert_or_get_ability_estimate(
        ability,
        course_id="course_1",
    ) == ability
    assert repository.get_ability_estimate(ability.estimate_id) == ability
    assert any(
        "INSERT INTO m8_irt_parameter_sets" in statement
        and approved.content_checksum() in parameters
        for statement, parameters in connection.executions
    )


def test_postgres_rejects_parameter_identity_conflicts() -> None:
    """Catch accepting changed content for an existing parameter identity."""

    stored = _shadow()
    candidate = stored.model_copy(update={"sample_size": 999})
    repository = PostgresM8Repository(
        FakePool(
            FakeConnection(
                _model_responder(run=_run(), parameter_sets=[stored])
            )
        )
    )

    with pytest.raises(PostgresOperationError, match="conflict"):
        repository.insert_or_get_parameter_set(
            candidate,
            course_id="course_1",
        )


def test_postgres_model_runtime_empty_reads_are_explicit() -> None:
    """Catch integrity errors being raised for ordinary missing records."""

    repository = PostgresM8Repository(
        FakePool(FakeConnection(lambda _statement, _parameters: None))
    )

    assert repository.get_calibration_run("missing") is None
    assert repository.get_calibration_run_course_id("missing") is None
    assert repository.get_parameter_set("missing") is None
    assert repository.get_parameter_set_course_id("missing") is None
    assert repository.list_parameter_sets(course_id="course_1") == []
    assert repository.get_calibration_review("missing") is None
    assert repository.get_ability_estimate("missing") is None
