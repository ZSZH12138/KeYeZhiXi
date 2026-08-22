"""Ordinal scoring and selective-review metrics for M7 evaluations."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ._common import (
    EvaluationInputError,
    load_jsonl_bytes,
    percentile,
    require_exact_keys,
    require_finite_number,
    require_local_regular_file,
    require_safe_id,
    require_sha256,
    sha256_bytes,
    wilson_interval,
)
from .datasets import EvaluationCase


_PREDICTION_FIELDS = frozenset(
    {"case_id", "predicted_score", "confidence", "review_required"}
)
_MAX_PREDICTION_BYTES = 128 * 1024 * 1024
_MAX_PREDICTIONS = 500_000


@dataclass(frozen=True, slots=True)
class ScorePrediction:
    """One score plus calibrated probability of exact correctness."""

    case_id: str
    predicted_score: float
    confidence: float
    review_required: bool

    def __post_init__(self) -> None:
        require_safe_id(self.case_id, field_name="prediction case_id")
        require_finite_number(
            self.predicted_score,
            field_name="predicted_score",
        )
        require_finite_number(
            self.confidence,
            field_name="confidence",
            minimum=0.0,
            maximum=1.0,
        )
        if type(self.review_required) is not bool:
            raise EvaluationInputError("review_required must be boolean")


def load_score_predictions(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[ScorePrediction, ...]:
    """Load checksum-pinned JSONL predictions without dataset text."""

    expected = require_sha256(expected_sha256, field_name="prediction SHA-256")
    checked = require_local_regular_file(path, max_bytes=_MAX_PREDICTION_BYTES)
    payload = checked.read_bytes()
    if sha256_bytes(payload) != expected:
        raise EvaluationInputError("prediction SHA-256 mismatch")
    raw_records = load_jsonl_bytes(
        payload,
        field_name="score predictions",
        max_records=_MAX_PREDICTIONS,
    )
    predictions: list[ScorePrediction] = []
    seen: set[str] = set()
    for raw in raw_records:
        require_exact_keys(raw, _PREDICTION_FIELDS, field_name="score prediction")
        prediction = ScorePrediction(
            case_id=require_safe_id(raw["case_id"], field_name="prediction case_id"),
            predicted_score=require_finite_number(
                raw["predicted_score"],
                field_name="predicted_score",
            ),
            confidence=require_finite_number(
                raw["confidence"],
                field_name="confidence",
                minimum=0.0,
                maximum=1.0,
            ),
            review_required=raw["review_required"],
        )
        if prediction.case_id in seen:
            raise EvaluationInputError("duplicate prediction case_id")
        seen.add(prediction.case_id)
        predictions.append(prediction)
    return tuple(predictions)


def evaluate_scoring(
    cases: Sequence[EvaluationCase],
    predictions: Sequence[ScorePrediction],
    *,
    material_error_threshold: float = 0.25,
    qwk_bins: int = 21,
    ece_bins: int = 10,
    bootstrap_samples: int = 500,
    confidence_level: float = 0.95,
    bootstrap_seed: int | None = None,
    worst_question_limit: int = 10,
) -> dict[str, Any]:
    """Evaluate scoring quality, calibration, and selective-review safety.

    Bootstrap resampling is clustered by ``question_id`` so answers to the same
    item remain dependent.  Reports contain no answer or prompt text.
    """

    threshold = require_finite_number(
        material_error_threshold,
        field_name="material_error_threshold",
        minimum=0.0,
        maximum=1.0,
    )
    if type(qwk_bins) is not int or not 3 <= qwk_bins <= 1001:
        raise EvaluationInputError("qwk_bins must be between 3 and 1001")
    if type(ece_bins) is not int or not 2 <= ece_bins <= 100:
        raise EvaluationInputError("ece_bins must be between 2 and 100")
    if type(bootstrap_samples) is not int or not 20 <= bootstrap_samples <= 20_000:
        raise EvaluationInputError("bootstrap_samples must be between 20 and 20000")
    if type(worst_question_limit) is not int or not 1 <= worst_question_limit <= 100:
        raise EvaluationInputError("worst_question_limit must be between 1 and 100")

    paired = _paired_cases(cases, predictions)
    point = _point_metrics(
        paired,
        material_error_threshold=threshold,
        qwk_bins=qwk_bins,
        ece_bins=ece_bins,
    )
    seed = (
        bootstrap_seed
        if bootstrap_seed is not None
        else _derived_seed(paired, threshold=threshold)
    )
    if type(seed) is not int:
        raise EvaluationInputError("bootstrap_seed must be an integer")
    bootstrap = _cluster_bootstrap(
        paired,
        material_error_threshold=threshold,
        qwk_bins=qwk_bins,
        ece_bins=ece_bins,
        samples=bootstrap_samples,
        confidence_level=confidence_level,
        seed=seed,
    )
    return {
        "schema_version": "m7_scoring_evaluation_v1",
        "case_count": len(paired),
        "question_count": len({case.question_id for case, _ in paired}),
        "material_error_threshold": threshold,
        "qwk_bins": qwk_bins,
        "ece_bins": ece_bins,
        "metrics": point,
        "binomial_confidence_intervals": _binomial_intervals(
            paired,
            material_error_threshold=threshold,
            confidence_level=confidence_level,
        ),
        "bootstrap_confidence_intervals": bootstrap,
        "selective_curve": _selective_curve(
            paired,
            material_error_threshold=threshold,
        ),
        "worst_questions": _worst_questions(
            paired,
            material_error_threshold=threshold,
            limit=worst_question_limit,
        ),
        "evaluation_checksum": _evaluation_checksum(paired),
    }


Pair = tuple[EvaluationCase, ScorePrediction]


def _paired_cases(
    cases: Sequence[EvaluationCase],
    predictions: Sequence[ScorePrediction],
) -> list[Pair]:
    if not cases:
        raise EvaluationInputError("evaluation cases must not be empty")
    case_by_id: dict[str, EvaluationCase] = {}
    for case in cases:
        if not isinstance(case, EvaluationCase):
            raise EvaluationInputError("evaluation case type is invalid")
        if case.case_id in case_by_id:
            raise EvaluationInputError("duplicate evaluation case_id")
        case_by_id[case.case_id] = case
    prediction_by_id: dict[str, ScorePrediction] = {}
    for prediction in predictions:
        if not isinstance(prediction, ScorePrediction):
            raise EvaluationInputError("score prediction type is invalid")
        if prediction.case_id in prediction_by_id:
            raise EvaluationInputError("duplicate prediction case_id")
        prediction_by_id[prediction.case_id] = prediction
    if set(case_by_id) != set(prediction_by_id):
        raise EvaluationInputError("predictions must match evaluation cases exactly")
    paired = [
        (case_by_id[case_id], prediction_by_id[case_id])
        for case_id in sorted(case_by_id)
    ]
    for case, prediction in paired:
        if not case.score_min <= prediction.predicted_score <= case.score_max:
            raise EvaluationInputError("predicted score is outside the item scale")
        position = (
            (prediction.predicted_score - case.score_min) / case.score_step
        )
        if not math.isclose(position, round(position), abs_tol=1e-9):
            raise EvaluationInputError("predicted score is not on the item scale")
    return paired


def _point_metrics(
    paired: Sequence[Pair],
    *,
    material_error_threshold: float,
    qwk_bins: int,
    ece_bins: int,
) -> dict[str, float | int | None]:
    errors = [_normalized_error(case, prediction) for case, prediction in paired]
    absolute = [abs(error) for error in errors]
    squared = [error * error for error in errors]
    exact = [_is_exact(case, prediction) for case, prediction in paired]
    adjacent = [_is_adjacent(case, prediction) for case, prediction in paired]
    material = [value >= material_error_threshold for value in absolute]
    reviewed = [prediction.review_required for _, prediction in paired]
    accepted_indexes = [index for index, flag in enumerate(reviewed) if not flag]
    material_indexes = [index for index, flag in enumerate(material) if flag]
    reviewed_material = sum(reviewed[index] for index in material_indexes)
    accepted_material = sum(material[index] for index in accepted_indexes)
    return {
        "normalized_mae": math.fsum(absolute) / len(paired),
        "normalized_rmse": math.sqrt(math.fsum(squared) / len(paired)),
        "qwk": _quadratic_weighted_kappa(paired, bins=qwk_bins),
        "exact_accuracy": sum(exact) / len(paired),
        "adjacent_accuracy": sum(adjacent) / len(paired),
        "brier_score": math.fsum(
            (prediction.confidence - float(outcome)) ** 2
            for (_, prediction), outcome in zip(paired, exact, strict=True)
        )
        / len(paired),
        "ece": _ece(paired, exact=exact, bins=ece_bins),
        "coverage": len(accepted_indexes) / len(paired),
        "review_rate": sum(reviewed) / len(paired),
        "selective_risk": (
            accepted_material / len(accepted_indexes)
            if accepted_indexes
            else None
        ),
        "material_error_rate": len(material_indexes) / len(paired),
        "material_error_review_recall": (
            reviewed_material / len(material_indexes)
            if material_indexes
            else None
        ),
        "material_error_count": len(material_indexes),
        "reviewed_material_error_count": reviewed_material,
    }


def _normalized_error(case: EvaluationCase, prediction: ScorePrediction) -> float:
    return (prediction.predicted_score - case.gold_score) / (
        case.score_max - case.score_min
    )


def _is_exact(case: EvaluationCase, prediction: ScorePrediction) -> bool:
    return math.isclose(
        prediction.predicted_score,
        case.gold_score,
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def _is_adjacent(case: EvaluationCase, prediction: ScorePrediction) -> bool:
    return abs(prediction.predicted_score - case.gold_score) <= case.score_step + 1e-9


def _quadratic_weighted_kappa(
    paired: Sequence[Pair],
    *,
    bins: int,
) -> float | None:
    gold = [
        _ordinal_bin(case.normalized_gold, bins=bins)
        for case, _ in paired
    ]
    predicted = [
        _ordinal_bin(
            (prediction.predicted_score - case.score_min)
            / (case.score_max - case.score_min),
            bins=bins,
        )
        for case, prediction in paired
    ]
    observed = [[0 for _ in range(bins)] for _ in range(bins)]
    gold_counts = [0 for _ in range(bins)]
    predicted_counts = [0 for _ in range(bins)]
    for gold_value, predicted_value in zip(gold, predicted, strict=True):
        observed[gold_value][predicted_value] += 1
        gold_counts[gold_value] += 1
        predicted_counts[predicted_value] += 1
    denominator_scale = float((bins - 1) ** 2)
    observed_weight = 0.0
    expected_weight = 0.0
    total = len(paired)
    for gold_value in range(bins):
        for predicted_value in range(bins):
            weight = ((gold_value - predicted_value) ** 2) / denominator_scale
            observed_weight += weight * observed[gold_value][predicted_value] / total
            expected_weight += (
                weight
                * gold_counts[gold_value]
                * predicted_counts[predicted_value]
                / (total * total)
            )
    if math.isclose(expected_weight, 0.0, abs_tol=1e-15):
        return 1.0 if math.isclose(observed_weight, 0.0, abs_tol=1e-15) else None
    return 1.0 - observed_weight / expected_weight


def _ordinal_bin(value: float, *, bins: int) -> int:
    return min(bins - 1, max(0, int(math.floor(value * (bins - 1) + 0.5))))


def _ece(
    paired: Sequence[Pair],
    *,
    exact: Sequence[bool],
    bins: int,
) -> float:
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for (_, prediction), outcome in zip(paired, exact, strict=True):
        index = min(bins - 1, int(prediction.confidence * bins))
        grouped[index].append((prediction.confidence, outcome))
    total = len(paired)
    return math.fsum(
        len(group) / total
        * abs(
            math.fsum(confidence for confidence, _ in group) / len(group)
            - sum(outcome for _, outcome in group) / len(group)
        )
        for group in grouped
        if group
    )


def _binomial_intervals(
    paired: Sequence[Pair],
    *,
    material_error_threshold: float,
    confidence_level: float,
) -> dict[str, dict[str, float | int] | None]:
    exact = sum(_is_exact(case, prediction) for case, prediction in paired)
    adjacent = sum(_is_adjacent(case, prediction) for case, prediction in paired)
    accepted = sum(not prediction.review_required for _, prediction in paired)
    material = [
        abs(_normalized_error(case, prediction)) >= material_error_threshold
        for case, prediction in paired
    ]
    reviewed_material = sum(
        outcome and prediction.review_required
        for (_, prediction), outcome in zip(paired, material, strict=True)
    )
    accepted_material = sum(
        outcome and not prediction.review_required
        for (_, prediction), outcome in zip(paired, material, strict=True)
    )
    return {
        "exact_accuracy": wilson_interval(
            exact,
            len(paired),
            confidence_level=confidence_level,
        ),
        "adjacent_accuracy": wilson_interval(
            adjacent,
            len(paired),
            confidence_level=confidence_level,
        ),
        "coverage": wilson_interval(
            accepted,
            len(paired),
            confidence_level=confidence_level,
        ),
        "selective_risk": wilson_interval(
            accepted_material,
            accepted,
            confidence_level=confidence_level,
        ),
        "material_error_review_recall": wilson_interval(
            reviewed_material,
            sum(material),
            confidence_level=confidence_level,
        ),
    }


def _cluster_bootstrap(
    paired: Sequence[Pair],
    *,
    material_error_threshold: float,
    qwk_bins: int,
    ece_bins: int,
    samples: int,
    confidence_level: float,
    seed: int,
) -> dict[str, dict[str, float | int] | None]:
    by_question: dict[str, list[Pair]] = defaultdict(list)
    for pair in paired:
        by_question[pair[0].question_id].append(pair)
    question_ids = sorted(by_question)
    rng = random.Random(seed)
    names = (
        "normalized_mae",
        "normalized_rmse",
        "qwk",
        "exact_accuracy",
        "adjacent_accuracy",
        "brier_score",
        "ece",
        "coverage",
        "selective_risk",
        "material_error_review_recall",
    )
    estimates: dict[str, list[float]] = {name: [] for name in names}
    for _ in range(samples):
        resampled: list[Pair] = []
        for _ in question_ids:
            selected = question_ids[rng.randrange(len(question_ids))]
            resampled.extend(by_question[selected])
        metrics = _point_metrics(
            resampled,
            material_error_threshold=material_error_threshold,
            qwk_bins=qwk_bins,
            ece_bins=ece_bins,
        )
        for name in names:
            value = metrics[name]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                estimates[name].append(float(value))
    tail = (1.0 - confidence_level) / 2.0
    return {
        name: (
            {
                "confidence_level": confidence_level,
                "samples": len(values),
                "lower": percentile(values, tail),
                "upper": percentile(values, 1.0 - tail),
            }
            if values
            else None
        )
        for name, values in estimates.items()
    }


def _selective_curve(
    paired: Sequence[Pair],
    *,
    material_error_threshold: float,
) -> list[dict[str, float | int | None]]:
    thresholds = [index / 20.0 for index in range(21)]
    result: list[dict[str, float | int | None]] = []
    for threshold in thresholds:
        accepted = [
            (case, prediction)
            for case, prediction in paired
            if prediction.confidence >= threshold
        ]
        material_count = sum(
            abs(_normalized_error(case, prediction)) >= material_error_threshold
            for case, prediction in accepted
        )
        result.append(
            {
                "minimum_confidence": threshold,
                "accepted_count": len(accepted),
                "coverage": len(accepted) / len(paired),
                "selective_risk": (
                    material_count / len(accepted) if accepted else None
                ),
            }
        )
    return result


def _worst_questions(
    paired: Sequence[Pair],
    *,
    material_error_threshold: float,
    limit: int,
) -> list[dict[str, float | int | str | None]]:
    by_question: dict[str, list[Pair]] = defaultdict(list)
    for pair in paired:
        by_question[pair[0].question_id].append(pair)
    rows: list[dict[str, float | int | str | None]] = []
    for question_id, values in by_question.items():
        absolute = [abs(_normalized_error(*pair)) for pair in values]
        material = [value >= material_error_threshold for value in absolute]
        reviewed_material = sum(
            is_material and prediction.review_required
            for (_, prediction), is_material in zip(values, material, strict=True)
        )
        rows.append(
            {
                "question_id": question_id,
                "case_count": len(values),
                "normalized_mae": math.fsum(absolute) / len(values),
                "material_error_rate": sum(material) / len(values),
                "material_error_review_recall": (
                    reviewed_material / sum(material) if any(material) else None
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            -float(row["normalized_mae"]),
            -float(row["material_error_rate"]),
            str(row["question_id"]),
        )
    )
    return rows[:limit]


def _derived_seed(paired: Sequence[Pair], *, threshold: float) -> int:
    digest = hashlib.sha256()
    digest.update(format(threshold, ".12g").encode("ascii"))
    for case, prediction in paired:
        digest.update(
            (
                f"|{case.case_id}|{case.question_id}|{case.gold_score:.12g}|"
                f"{prediction.predicted_score:.12g}|{prediction.confidence:.12g}|"
                f"{int(prediction.review_required)}"
            ).encode("utf-8")
        )
    return int.from_bytes(digest.digest()[:8], "big")


def _evaluation_checksum(paired: Iterable[Pair]) -> str:
    payload = [
        {
            "case_id": case.case_id,
            "question_id": case.question_id,
            "gold_score": case.gold_score,
            "predicted_score": prediction.predicted_score,
            "confidence": prediction.confidence,
            "review_required": prediction.review_required,
        }
        for case, prediction in paired
    ]
    import json

    return sha256_bytes(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


__all__ = [
    "ScorePrediction",
    "evaluate_scoring",
    "load_score_predictions",
]
