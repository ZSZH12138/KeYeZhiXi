"""M9 must turn real calibration evidence into a usable quality gate."""

from __future__ import annotations

from datetime import UTC, datetime

from course_insight.contracts.learning_models import (
    CalibrationRunResult,
    IRTItemParameters,
    IRTParameterSet,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


def _service() -> M9TeacherAnalyticsService:
    return M9TeacherAnalyticsService(object(), object(), object())


def _shadow_run(*, boundary_parameters: bool = False) -> CalibrationRunResult:
    return CalibrationRunResult(
        run_id="calibration_quality_1",
        parameter_set=IRTParameterSet(
            parameter_set_id="irt_shadow_quality_1",
            model_type="2PL",
            version="irt-quality-v1",
            item_parameters=[
                IRTItemParameters(
                    item_id=f"item_{index}",
                    item_version="1.0.0",
                    discrimination=(3.0 if boundary_parameters else 1.1 + index * 0.02),
                    difficulty=(4.0 if boundary_parameters else -0.5 + index * 0.1),
                    guessing=0.0,
                    sample_size=200,
                )
                for index in range(10)
            ],
            sample_size=200,
            status="shadow",
            created_at=NOW,
        ),
        converged=True,
        metrics={
            "log_likelihood": -820.0,
            "aic": 1680.0,
            "bic": 1745.0,
            "iteration_count": 24.0,
        },
        status="shadow",
        failure_code=None,
        generated_at=NOW,
    )


def test_converged_shadow_calibration_produces_ready_quality_evidence() -> None:
    run = _shadow_run()

    report = _service().build_model_quality_report(run, NOW)

    assert report.subject_ref == run.run_id
    assert report.observation_count == 200
    assert report.status == "ready"
    assert report.metrics["sample_coverage"] == 1.0
    assert report.metrics["parameter_stability"] == 1.0
    assert report.metrics["mean_fisher_information_at_zero"] > 0.0


def test_boundary_saturated_parameters_fail_the_quality_gate() -> None:
    report = _service().build_model_quality_report(
        _shadow_run(boundary_parameters=True),
        NOW,
    )

    assert report.status == "failed"
    assert report.metrics["parameter_stability"] == 0.0


def test_insufficient_calibration_remains_metric_free() -> None:
    empty_parameters = IRTParameterSet(
        parameter_set_id="irt_insufficient_quality",
        model_type="2PL",
        version="irt-insufficient-quality",
        item_parameters=[],
        sample_size=0,
        status="empty",
        created_at=NOW,
    )
    run = CalibrationRunResult(
        run_id="calibration_insufficient_quality",
        parameter_set=empty_parameters,
        converged=False,
        metrics={},
        status="failed",
        failure_code="INSUFFICIENT_CALIBRATION_DATA",
        generated_at=NOW,
    )

    report = _service().build_model_quality_report(run, NOW)

    assert report.subject_ref == run.run_id
    assert report.status == "insufficient_data"
    assert report.metrics == {}


def test_nonconverged_calibration_reports_failed_evidence() -> None:
    empty_parameters = IRTParameterSet(
        parameter_set_id="irt_failed_quality",
        model_type="2PL",
        version="irt-failed-quality",
        item_parameters=[],
        sample_size=0,
        status="empty",
        created_at=NOW,
    )
    run = CalibrationRunResult(
        run_id="calibration_failed_quality",
        parameter_set=empty_parameters,
        converged=False,
        metrics={"optimizer_exit_code": 2.0},
        status="failed",
        failure_code="CALIBRATION_DID_NOT_CONVERGE",
        generated_at=NOW,
    )

    report = _service().build_model_quality_report(run, NOW)

    assert report.status == "failed"
    assert report.metrics == {"optimizer_exit_code": 2.0}
