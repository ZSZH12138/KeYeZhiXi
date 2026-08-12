"""Marginal-maximum-likelihood two-parameter logistic IRT calibration."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logsumexp

from course_insight.contracts.learning_models import (
    AbilityEstimate,
    CalibrationRunResult,
    IRTItemParameters,
    IRTParameterSet,
    LearningObservation,
)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("IRT timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _evidence_created_at(
    observations: Sequence[LearningObservation],
    requested_at: datetime,
) -> datetime:
    if not observations:
        return requested_at.astimezone(UTC)
    return max(item.occurred_at for item in observations).astimezone(UTC)


def _run_digest(
    *,
    parameter_set_id: str,
    status: str,
    failure_code: str | None,
    requested_at: datetime,
) -> str:
    payload = {
        "parameter_set_id": parameter_set_id,
        "status": status,
        "failure_code": failure_code,
        "requested_at": _utc_iso(requested_at),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TwoPLCalibrator:
    """Fit 2PL item parameters while fixing ability to a standard normal scale."""

    def __init__(
        self,
        *,
        min_students: int = 200,
        min_responses_per_item: int = 50,
        min_items: int = 10,
        quadrature_points: int = 21,
        max_iterations: int = 200,
        likelihood_tolerance: float = 1e-6,
        parameter_tolerance: float = 1e-4,
    ) -> None:
        if (
            min_students < 1
            or min_responses_per_item < 1
            or min_items < 1
            or quadrature_points < 3
            or max_iterations < 1
        ):
            raise ValueError("2PL calibration thresholds must be positive")
        self._min_students = min_students
        self._min_responses_per_item = min_responses_per_item
        self._min_items = min_items
        self._max_iterations = max_iterations
        self._likelihood_tolerance = likelihood_tolerance
        self._parameter_tolerance = parameter_tolerance
        roots, weights = np.polynomial.hermite.hermgauss(quadrature_points)
        self._theta = np.sqrt(2.0) * roots
        self._prior_weights = weights / np.sqrt(np.pi)
        self._log_prior_weights = np.log(self._prior_weights)

    @staticmethod
    def probability(
        *,
        theta: float,
        discrimination: float,
        difficulty: float,
    ) -> float:
        """Return the 2PL probability of a correct response."""

        return float(expit(discrimination * (theta - difficulty)))

    @classmethod
    def information(
        cls,
        *,
        theta: float,
        discrimination: float,
        difficulty: float,
    ) -> float:
        """Return 2PL Fisher information at one ability value."""

        probability = cls.probability(
            theta=theta,
            discrimination=discrimination,
            difficulty=difficulty,
        )
        return discrimination * discrimination * probability * (1.0 - probability)

    def fit(
        self,
        observations: list[LearningObservation],
        requested_at: datetime,
    ) -> CalibrationRunResult:
        """Fit a shadow 2PL parameter set with marginal maximum likelihood."""

        _utc_iso(requested_at)
        requested_at = requested_at.astimezone(UTC)
        prepared = self._prepare(observations)
        if prepared is None:
            return self._failure_result(observations, requested_at)
        learner_ids, item_keys, response_matrix, sample_sizes = prepared
        item_count = len(item_keys)
        discrimination = np.ones(item_count, dtype=float)
        correct_rates = np.nanmean(response_matrix, axis=0)
        correct_rates = np.clip(correct_rates, 0.02, 0.98)
        difficulty = np.clip(
            -np.log(correct_rates / (1.0 - correct_rates)),
            -4.0,
            4.0,
        )
        previous_likelihood: float | None = None
        converged = False
        log_likelihood = float("-inf")

        for iteration in range(1, self._max_iterations + 1):
            posterior, _ = self._expectation(
                response_matrix,
                discrimination,
                difficulty,
            )
            next_discrimination = discrimination.copy()
            next_difficulty = difficulty.copy()
            for item_index in range(item_count):
                mask = ~np.isnan(response_matrix[:, item_index])
                outcomes = response_matrix[mask, item_index]
                weights = posterior[mask]
                expected_total = weights.sum(axis=0)
                expected_correct = (weights * outcomes[:, None]).sum(axis=0)
                initial = np.array(
                    [discrimination[item_index], difficulty[item_index]],
                    dtype=float,
                )
                optimized = minimize(
                    self._item_objective,
                    initial,
                    args=(expected_correct, expected_total),
                    method="L-BFGS-B",
                    jac=True,
                    bounds=((0.20, 3.00), (-4.00, 4.00)),
                    options={"maxiter": 100, "ftol": 1e-12, "gtol": 1e-8},
                )
                next_discrimination[item_index] = optimized.x[0]
                next_difficulty[item_index] = optimized.x[1]
            _, log_likelihood = self._expectation(
                response_matrix,
                next_discrimination,
                next_difficulty,
            )
            maximum_change = float(
                max(
                    np.max(np.abs(next_discrimination - discrimination)),
                    np.max(np.abs(next_difficulty - difficulty)),
                )
            )
            likelihood_change = (
                math.inf
                if previous_likelihood is None
                else abs(log_likelihood - previous_likelihood)
            )
            discrimination = next_discrimination
            difficulty = next_difficulty
            if (
                likelihood_change < self._likelihood_tolerance
                and maximum_change < self._parameter_tolerance
            ):
                converged = True
                break
            previous_likelihood = log_likelihood

        parameter_count = 2 * item_count
        sample_size = len(learner_ids)
        metrics = {
            "log_likelihood": float(log_likelihood),
            "aic": float(2 * parameter_count - 2 * log_likelihood),
            "bic": float(
                parameter_count * math.log(max(sample_size, 1))
                - 2 * log_likelihood
            ),
            "iteration_count": float(iteration),
        }
        payload = {
            "item_parameters": [
                {
                    "item_id": item_id,
                    "item_version": item_version,
                    "discrimination": round(float(discrimination[index]), 12),
                    "difficulty": round(float(difficulty[index]), 12),
                    "guessing": 0.0,
                    "sample_size": int(sample_sizes[index]),
                }
                for index, (item_id, item_version) in enumerate(item_keys)
            ],
            "sample_size": sample_size,
            "metrics": {key: round(value, 12) for key, value in metrics.items()},
            "converged": converged,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        evidence_created_at = _evidence_created_at(observations, requested_at)
        if not converged:
            parameter_set = IRTParameterSet(
                parameter_set_id=f"irt_failed_{digest[:24]}",
                model_type="2PL",
                version=f"irt-failed-{digest[:16]}",
                item_parameters=[],
                sample_size=0,
                status="empty",
                created_at=evidence_created_at,
            )
            run_digest = _run_digest(
                parameter_set_id=parameter_set.parameter_set_id,
                status="failed",
                failure_code="CALIBRATION_DID_NOT_CONVERGE",
                requested_at=requested_at,
            )
            return CalibrationRunResult(
                run_id=f"calibration_failed_{run_digest[:24]}",
                parameter_set=parameter_set,
                converged=False,
                metrics=metrics,
                status="failed",
                failure_code="CALIBRATION_DID_NOT_CONVERGE",
                generated_at=requested_at,
            )
        parameter_set = IRTParameterSet(
            parameter_set_id=f"irt_shadow_{digest[:24]}",
            model_type="2PL",
            version=f"irt-{digest[:16]}",
            item_parameters=[IRTItemParameters(**item) for item in payload["item_parameters"]],
            sample_size=sample_size,
            status="shadow",
            created_at=evidence_created_at,
        )
        run_digest = _run_digest(
            parameter_set_id=parameter_set.parameter_set_id,
            status="shadow",
            failure_code=None,
            requested_at=requested_at,
        )
        return CalibrationRunResult(
            run_id=f"calibration_{run_digest[:24]}",
            parameter_set=parameter_set,
            converged=True,
            metrics=metrics,
            status="shadow",
            failure_code=None,
            generated_at=requested_at,
        )

    def estimate_ability(
        self,
        parameter_set: IRTParameterSet,
        responses: list[LearningObservation],
    ) -> AbilityEstimate:
        """Estimate finite EAP ability and posterior standard error."""

        if parameter_set.status != "approved":
            raise ValueError("ability estimation requires approved IRT parameters")
        if parameter_set.model_type != "2PL":
            raise ValueError("ability estimation requires a 2PL parameter set")
        if not responses:
            raise ValueError("ability estimation requires at least one response")
        learner_ids = {item.learner_id for item in responses}
        if len(learner_ids) != 1:
            raise ValueError("ability responses must belong to one learner")
        parameters = {
            (item.item_id, item.item_version): item
            for item in parameter_set.item_parameters
        }
        log_posterior = self._log_prior_weights.copy()
        used = 0
        for response in sorted(
            responses,
            key=lambda item: (item.occurred_at, item.attempt_id, item.observation_id),
        ):
            item = parameters.get((response.item_id, response.item_version))
            if item is None:
                continue
            probabilities = expit(
                item.discrimination * (self._theta - item.difficulty)
            )
            probabilities = np.clip(probabilities, 1e-12, 1.0 - 1e-12)
            log_posterior += (
                np.log(probabilities)
                if response.response_outcome == "correct"
                else np.log1p(-probabilities)
            )
            used += 1
        if used == 0:
            raise ValueError("responses do not match approved IRT parameters")
        posterior = np.exp(log_posterior - logsumexp(log_posterior))
        theta = float(np.dot(posterior, self._theta))
        variance = float(np.dot(posterior, (self._theta - theta) ** 2))
        standard_error = math.sqrt(max(variance, 1e-12))
        learner_id = next(iter(learner_ids))
        digest = hashlib.sha256(
            (
                f"{parameter_set.parameter_set_id}:{learner_id}:"
                f"{','.join(sorted(item.observation_id for item in responses))}"
            ).encode("utf-8")
        ).hexdigest()
        return AbilityEstimate(
            estimate_id=f"ability_{digest[:24]}",
            learner_id=learner_id,
            parameter_set_id=parameter_set.parameter_set_id,
            theta=theta,
            standard_error=standard_error,
            status="estimated",
            estimated_at=max(item.occurred_at for item in responses),
        )

    def _prepare(
        self,
        observations: Sequence[LearningObservation],
    ) -> tuple[list[str], list[tuple[str, str]], np.ndarray, np.ndarray] | None:
        if not observations:
            return None
        current_by_audit: dict[tuple[str, int], LearningObservation] = {}
        for observation in observations:
            key = (observation.source_audit_id, observation.source_audit_version)
            existing = current_by_audit.get(key)
            if existing is not None and existing != observation:
                return None
            current_by_audit[key] = observation
        governed = sorted(
            current_by_audit.values(),
            key=lambda item: (
                item.learner_id,
                item.occurred_at,
                item.attempt_id,
                item.observation_id,
            ),
        )
        learner_ids = sorted({item.learner_id for item in governed})
        item_keys = sorted({(item.item_id, item.item_version) for item in governed})
        if len(learner_ids) < self._min_students:
            return None
        learner_index = {value: index for index, value in enumerate(learner_ids)}
        item_index = {value: index for index, value in enumerate(item_keys)}
        matrix = np.full((len(learner_ids), len(item_keys)), np.nan, dtype=float)
        for observation in governed:
            row = learner_index[observation.learner_id]
            column = item_index[(observation.item_id, observation.item_version)]
            value = 1.0 if observation.response_outcome == "correct" else 0.0
            if not np.isnan(matrix[row, column]) and matrix[row, column] != value:
                return None
            matrix[row, column] = value
        valid_columns: list[int] = []
        for column in range(matrix.shape[1]):
            values = matrix[:, column]
            values = values[~np.isnan(values)]
            if (
                values.size >= self._min_responses_per_item
                and np.any(values == 0.0)
                and np.any(values == 1.0)
            ):
                valid_columns.append(column)
        if len(valid_columns) < self._min_items:
            return None
        filtered_matrix = matrix[:, valid_columns]
        filtered_keys = [item_keys[index] for index in valid_columns]
        sample_sizes = np.sum(~np.isnan(filtered_matrix), axis=0)
        return learner_ids, filtered_keys, filtered_matrix, sample_sizes

    def _expectation(
        self,
        response_matrix: np.ndarray,
        discrimination: np.ndarray,
        difficulty: np.ndarray,
    ) -> tuple[np.ndarray, float]:
        log_joint = np.broadcast_to(
            self._log_prior_weights,
            (response_matrix.shape[0], self._theta.size),
        ).copy()
        for item_index in range(response_matrix.shape[1]):
            mask = ~np.isnan(response_matrix[:, item_index])
            if not np.any(mask):
                continue
            probabilities = expit(
                discrimination[item_index]
                * (self._theta - difficulty[item_index])
            )
            probabilities = np.clip(probabilities, 1e-12, 1.0 - 1e-12)
            outcomes = response_matrix[mask, item_index]
            log_joint[mask] += (
                outcomes[:, None] * np.log(probabilities)[None, :]
                + (1.0 - outcomes[:, None])
                * np.log1p(-probabilities)[None, :]
            )
        normalizers = logsumexp(log_joint, axis=1)
        posterior = np.exp(log_joint - normalizers[:, None])
        return posterior, float(np.sum(normalizers))

    def _item_objective(
        self,
        parameters: np.ndarray,
        expected_correct: np.ndarray,
        expected_total: np.ndarray,
    ) -> tuple[float, np.ndarray]:
        discrimination, difficulty = parameters
        probabilities = expit(discrimination * (self._theta - difficulty))
        probabilities = np.clip(probabilities, 1e-12, 1.0 - 1e-12)
        log_likelihood = np.sum(
            expected_correct * np.log(probabilities)
            + (expected_total - expected_correct) * np.log1p(-probabilities)
        )
        residual = expected_correct - expected_total * probabilities
        gradient = np.array(
            [
                np.sum(residual * (self._theta - difficulty)),
                np.sum(residual * (-discrimination)),
            ],
            dtype=float,
        )
        return -float(log_likelihood), -gradient

    @staticmethod
    def _failure_result(
        observations: Sequence[LearningObservation],
        requested_at: datetime,
    ) -> CalibrationRunResult:
        evidence_created_at = _evidence_created_at(observations, requested_at)
        identity_payload = {
            "observations": [
                item.model_dump(mode="json")
                for item in sorted(
                    observations,
                    key=lambda item: (
                        item.source_audit_id,
                        item.source_audit_version,
                        item.observation_id,
                    ),
                )
            ],
            "empty_request_time": (
                _utc_iso(requested_at) if not observations else None
            ),
        }
        digest = hashlib.sha256(
            json.dumps(
                identity_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        parameter_set = IRTParameterSet(
            parameter_set_id=f"irt_insufficient_{digest[:24]}",
            model_type="2PL",
            version=f"irt-insufficient-{digest[:16]}",
            item_parameters=[],
            sample_size=0,
            status="empty",
            created_at=evidence_created_at,
        )
        run_digest = _run_digest(
            parameter_set_id=parameter_set.parameter_set_id,
            status="failed",
            failure_code="INSUFFICIENT_CALIBRATION_DATA",
            requested_at=requested_at,
        )
        return CalibrationRunResult(
            run_id=f"calibration_insufficient_{run_digest[:24]}",
            parameter_set=parameter_set,
            converged=False,
            metrics={},
            status="failed",
            failure_code="INSUFFICIENT_CALIBRATION_DATA",
            generated_at=requested_at,
        )
