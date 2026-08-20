"""PAV fitting helpers for data-only M7 review-selector artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ._common import (
    EvaluationInputError,
    canonical_json_bytes,
    load_jsonl_bytes,
    require_exact_keys,
    require_finite_number,
    require_local_regular_file,
    require_safe_id,
    require_sha256,
    sha256_bytes,
)


FEATURE_NAMES = (
    "raw_risk",
    "answer_characters",
    "criterion_count",
    "evidence_chunk_count",
    "evidence_characters",
    "normalized_total_score",
    "missing_concept_fraction",
    "cited_criterion_fraction",
)
_OBSERVATION_FIELDS = frozenset(
    {
        "case_id",
        "split",
        "rubric_ref",
        "raw_risk",
        "material_error",
        "features",
    }
)


@dataclass(frozen=True, slots=True)
class SelectorObservation:
    case_id: str
    split: str
    rubric_ref: str
    raw_risk: float
    material_error: bool
    features: Mapping[str, float]


def load_selector_observations(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[SelectorObservation, ...]:
    """Load checksum-pinned, text-free calibration observations."""

    expected = require_sha256(expected_sha256, field_name="selector data SHA-256")
    checked = require_local_regular_file(path, max_bytes=128 * 1024 * 1024)
    payload = checked.read_bytes()
    if sha256_bytes(payload) != expected:
        raise EvaluationInputError("selector observation SHA-256 mismatch")
    rows = load_jsonl_bytes(
        payload,
        field_name="selector observations",
        max_records=500_000,
    )
    result: list[SelectorObservation] = []
    seen: set[str] = set()
    for row in rows:
        require_exact_keys(row, _OBSERVATION_FIELDS, field_name="selector observation")
        case_id = require_safe_id(row["case_id"], field_name="case_id")
        if case_id in seen:
            raise EvaluationInputError("duplicate selector case_id")
        seen.add(case_id)
        split = row["split"]
        if split not in {"train", "calibration", "test"}:
            raise EvaluationInputError("selector split is invalid")
        rubric_ref = require_safe_id(row["rubric_ref"], field_name="rubric_ref")
        if type(row["material_error"]) is not bool:
            raise EvaluationInputError("material_error must be boolean")
        raw_features = row["features"]
        require_exact_keys(raw_features, set(FEATURE_NAMES), field_name="selector features")
        features = {
            name: require_finite_number(
                raw_features[name],
                field_name=name,
                minimum=0.0,
            )
            for name in FEATURE_NAMES
        }
        raw_risk = require_finite_number(
            row["raw_risk"], field_name="raw_risk", minimum=0.0, maximum=1.0
        )
        if features["raw_risk"] != raw_risk:
            raise EvaluationInputError("raw_risk and feature value disagree")
        result.append(
            SelectorObservation(
                case_id=case_id,
                split=split,
                rubric_ref=rubric_ref,
                raw_risk=raw_risk,
                material_error=row["material_error"],
                features=features,
            )
        )
    return tuple(result)


def fit_pav_selector(
    observations: Sequence[SelectorObservation],
    *,
    selector_id: str,
    selector_version: str,
    bindings: Mapping[str, str],
    acceptance_raw_risk_upper: float,
    maximum_calibrated_risk: float,
    maximum_acceptance_error_rate: float,
    confidence_level: float = 0.95,
    minimum_calibration_samples: int = 100,
    minimum_acceptance_samples: int = 30,
    minimum_segment_samples: int = 10,
) -> dict[str, Any]:
    """Fit isotonic material-error risk using calibration rows only."""

    require_safe_id(selector_id, field_name="selector_id")
    require_safe_id(selector_version, field_name="selector_version")
    calibration = [item for item in observations if item.split == "calibration"]
    if len(calibration) < minimum_calibration_samples:
        raise EvaluationInputError("insufficient calibration observations")
    if len({item.case_id for item in calibration}) != len(calibration):
        raise EvaluationInputError("duplicate calibration case_id")
    segments = _pav_segments(calibration)
    eligible = [
        item
        for item in segments
        if item["raw_risk_upper"] <= acceptance_raw_risk_upper
    ]
    if not eligible:
        raise EvaluationInputError("acceptance threshold selects no PAV segment")
    frozen_threshold = eligible[-1]["raw_risk_upper"]
    ranges = {
        name: {
            "minimum": min(float(item.features[name]) for item in calibration),
            "maximum": max(float(item.features[name]) for item in calibration),
        }
        for name in FEATURE_NAMES
    }
    return {
        "schema_version": "m7-review-selector-artifact-v1",
        "selector_id": selector_id,
        "selector_version": selector_version,
        "bindings": dict(bindings),
        "calibration": {"method": "pav_isotonic", "segments": segments},
        "gates": {
            "minimum_calibration_samples": minimum_calibration_samples,
            "minimum_acceptance_samples": minimum_acceptance_samples,
            "minimum_segment_samples": minimum_segment_samples,
            "acceptance_raw_risk_upper": frozen_threshold,
            "maximum_calibrated_risk": maximum_calibrated_risk,
            "maximum_acceptance_error_rate": maximum_acceptance_error_rate,
            "confidence_level": confidence_level,
            "upper_bound_method": "wilson_one_sided",
            "approved_rubric_refs": sorted(
                {item.rubric_ref for item in calibration}
            ),
            "ood_ranges": ranges,
        },
    }


def predict_pav(artifact: Mapping[str, Any], raw_risk: float) -> float:
    """Apply only the fitted monotone segments, useful for offline checks."""

    risk = require_finite_number(
        raw_risk, field_name="raw_risk", minimum=0.0, maximum=1.0
    )
    for segment in artifact["calibration"]["segments"]:
        if risk <= float(segment["raw_risk_upper"]):
            return float(segment["calibrated_risk"])
    raise EvaluationInputError("PAV artifact does not cover raw risk")


def artifact_bytes(artifact: Mapping[str, Any]) -> bytes:
    """Return canonical bytes accepted by the production selector loader."""

    return canonical_json_bytes(dict(artifact))


def _pav_segments(
    observations: Sequence[SelectorObservation],
) -> list[dict[str, float | int]]:
    grouped: list[dict[str, float | int]] = []
    for item in sorted(observations, key=lambda value: (value.raw_risk, value.case_id)):
        if grouped and float(grouped[-1]["raw_risk_upper"]) == item.raw_risk:
            grouped[-1]["sample_count"] = int(grouped[-1]["sample_count"]) + 1
            grouped[-1]["error_count"] = int(grouped[-1]["error_count"]) + int(item.material_error)
        else:
            grouped.append(
                {
                    "raw_risk_upper": item.raw_risk,
                    "sample_count": 1,
                    "error_count": int(item.material_error),
                }
            )
    blocks = grouped
    index = 0
    while index < len(blocks) - 1:
        left = int(blocks[index]["error_count"]) / int(blocks[index]["sample_count"])
        right = int(blocks[index + 1]["error_count"]) / int(blocks[index + 1]["sample_count"])
        if left <= right:
            index += 1
            continue
        blocks[index] = {
            "raw_risk_upper": blocks[index + 1]["raw_risk_upper"],
            "sample_count": int(blocks[index]["sample_count"]) + int(blocks[index + 1]["sample_count"]),
            "error_count": int(blocks[index]["error_count"]) + int(blocks[index + 1]["error_count"]),
        }
        del blocks[index + 1]
        index = max(0, index - 1)
    blocks[-1]["raw_risk_upper"] = 1.0
    return [
        {
            "raw_risk_upper": float(item["raw_risk_upper"]),
            "calibrated_risk": int(item["error_count"]) / int(item["sample_count"]),
            "sample_count": int(item["sample_count"]),
            "error_count": int(item["error_count"]),
        }
        for item in blocks
    ]


__all__ = [
    "FEATURE_NAMES",
    "SelectorObservation",
    "artifact_bytes",
    "fit_pav_selector",
    "load_selector_observations",
    "predict_pav",
]
