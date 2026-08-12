"""Deterministic quality evidence for M8 2PL calibration runs."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from course_insight.contracts.learning_models import (
    CalibrationRunResult,
    ModelQualityReport,
)


_REQUIRED_RUN_METRICS = frozenset(
    {"log_likelihood", "aic", "bic", "iteration_count"}
)
_BOUNDARY_TOLERANCE = 1e-6
_MIN_SAMPLE_COVERAGE = 0.25
_MIN_MEAN_INFORMATION = 0.01
_MAX_BOUNDARY_RATE = 0.25


def build_calibration_quality_report(
    calibration: CalibrationRunResult,
    requested_at: datetime,
) -> ModelQualityReport:
    """Evaluate persisted calibration evidence without inventing metrics."""

    if calibration.failure_code == "INSUFFICIENT_CALIBRATION_DATA" or (
        calibration.status == "empty"
    ):
        return _report(
            calibration,
            requested_at,
            metrics={},
            status="insufficient_data",
        )
    if calibration.status != "shadow" or not calibration.converged:
        return _report(
            calibration,
            requested_at,
            metrics=_finite_metrics(calibration.metrics),
            status="failed",
        )

    parameters = calibration.parameter_set
    items = list(parameters.item_parameters)
    sample_coverage = (
        0.0
        if not items or parameters.sample_size <= 0
        else min(item.sample_size for item in items) / parameters.sample_size
    )
    boundary_count = sum(
        int(_at_boundary(item.discrimination, 0.20, 3.00))
        + int(_at_boundary(item.difficulty, -4.00, 4.00))
        for item in items
    )
    boundary_rate = (
        1.0 if not items else boundary_count / (2.0 * len(items))
    )
    mean_information = (
        0.0
        if not items
        else math.fsum(
            _information_at_zero(
                item.discrimination,
                item.difficulty,
            )
            for item in items
        )
        / len(items)
    )
    run_metrics = _finite_metrics(calibration.metrics)
    metrics = {
        **run_metrics,
        "item_count": float(len(items)),
        "sample_coverage": float(sample_coverage),
        "parameter_stability": float(1.0 - boundary_rate),
        "mean_fisher_information_at_zero": float(mean_information),
    }
    ready = (
        parameters.model_type == "2PL"
        and parameters.status == "shadow"
        and _REQUIRED_RUN_METRICS <= run_metrics.keys()
        and run_metrics["iteration_count"] >= 1.0
        and sample_coverage >= _MIN_SAMPLE_COVERAGE
        and boundary_rate <= _MAX_BOUNDARY_RATE
        and mean_information >= _MIN_MEAN_INFORMATION
    )
    return _report(
        calibration,
        requested_at,
        metrics=metrics,
        status="ready" if ready else "failed",
    )


def _finite_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {
        key: float(value)
        for key, value in metrics.items()
        if math.isfinite(value)
    }


def _at_boundary(value: float, lower: float, upper: float) -> bool:
    return (
        abs(value - lower) <= _BOUNDARY_TOLERANCE
        or abs(value - upper) <= _BOUNDARY_TOLERANCE
    )


def _information_at_zero(discrimination: float, difficulty: float) -> float:
    probability = 1.0 / (1.0 + math.exp(discrimination * difficulty))
    return discrimination * discrimination * probability * (1.0 - probability)


def _report(
    calibration: CalibrationRunResult,
    requested_at: datetime,
    *,
    metrics: dict[str, float],
    status: Literal["insufficient_data", "ready", "failed"],
) -> ModelQualityReport:
    return ModelQualityReport(
        report_id=f"quality_{calibration.run_id}",
        subject_ref=calibration.run_id,
        metrics=metrics,
        observation_count=calibration.parameter_set.sample_size,
        status=status,
        generated_at=requested_at,
    )


__all__ = ["build_calibration_quality_report"]
