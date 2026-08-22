from __future__ import annotations

import hashlib
import json

import pytest

from course_insight.modules.m7_local_model.policy import DEEPSEEK_MODEL_CANDIDATES
from scripts.evaluation.datasets import load_local_dataset_package, split_by_question
from scripts.evaluation.live_matrix import (
    LiveCaseResult,
    LiveEvaluationCase,
    run_live_matrix,
)
from scripts.evaluation.metrics import ScorePrediction, evaluate_scoring
from scripts.evaluation.prepare import prepare_local_dataset_package
from scripts.evaluation.selector import (
    FEATURE_NAMES,
    SelectorObservation,
    artifact_bytes,
    fit_pav_selector,
    predict_pav,
)


def _records() -> bytes:
    rows = [
        {
            "case_id": f"case_{index}",
            "question_id": f"question_{index}",
            "answer": f"local answer {index}",
            "gold_score": float(index % 3),
            "score_min": 0.0,
            "score_max": 2.0,
            "score_step": 1.0,
        }
        for index in range(1, 4)
    ]
    return b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for row in rows
    )


def test_local_dataset_prepare_is_pinned_and_question_disjoint(tmp_path) -> None:
    source = tmp_path / "normalized.jsonl"
    source.write_bytes(_records())
    output = tmp_path / "package"
    digest = prepare_local_dataset_package(
        normalized_records_path=source,
        output_dir=output,
        dataset_family="CESA_ASAP_ZH",
        dataset_id="cesa_local",
        dataset_version="1",
    )
    package = load_local_dataset_package(
        output,
        expected_package_sha256=digest,
    )
    assert package.license == "NOASSERTION"
    assert not package.license_confirmed
    split = split_by_question(package)
    groups = (
        set(split.train_question_ids),
        set(split.calibration_question_ids),
        set(split.test_question_ids),
    )
    assert all(left.isdisjoint(right) for left in groups for right in groups if left is not right)


def test_scoring_metrics_cover_accuracy_calibration_and_selective_risk(tmp_path) -> None:
    source = tmp_path / "normalized.jsonl"
    source.write_bytes(_records())
    output = tmp_path / "package"
    digest = prepare_local_dataset_package(
        normalized_records_path=source,
        output_dir=output,
        dataset_family="CESA_ASAP_ZH",
        dataset_id="cesa_local",
        dataset_version="1",
    )
    cases = load_local_dataset_package(output, expected_package_sha256=digest).cases
    predictions = [
        ScorePrediction(
            case_id=case.case_id,
            predicted_score=case.gold_score,
            confidence=1.0,
            review_required=False,
        )
        for case in cases
    ]
    report = evaluate_scoring(cases, predictions, bootstrap_samples=20)
    assert report["metrics"]["normalized_mae"] == 0.0
    assert report["metrics"]["exact_accuracy"] == 1.0
    assert report["metrics"]["selective_risk"] == 0.0
    assert report["metrics"]["brier_score"] == 0.0


def test_pav_fit_is_monotone_and_exports_data_only_artifact() -> None:
    observations = []
    for index, risk in enumerate((0.05, 0.1, 0.2, 0.4, 0.7, 0.9), start=1):
        features = {name: 1.0 for name in FEATURE_NAMES}
        features["raw_risk"] = risk
        observations.append(
            SelectorObservation(
                case_id=f"case_{index}",
                split="calibration",
                rubric_ref="rubric_1:1",
                raw_risk=risk,
                material_error=False,
                features=features,
            )
        )
    binding = {
        "calibration_data_id": "course_gold_1",
        "calibration_data_sha256": "c" * 64,
        "calibration_data_version": "1",
        "execution_policy_version": "m7-governed-v2",
        "feature_schema_version": "m7-review-features-v1",
        "model_name": "deepseek-v4-flash",
        "model_version": "runtime-api",
        "privacy_policy_version": "m7-outbound-privacy-v2",
        "prompt_id": "m7-rubric-scoring-json",
        "prompt_version": "5.0.0",
        "split_id": "question_split_1",
        "split_sha256": "d" * 64,
        "thinking_mode": "non_thinking",
    }
    artifact = fit_pav_selector(
        observations,
        selector_id="selector_1",
        selector_version="1",
        bindings=binding,
        acceptance_raw_risk_upper=1.0,
        maximum_calibrated_risk=0.1,
        maximum_acceptance_error_rate=0.8,
        minimum_calibration_samples=6,
        minimum_acceptance_samples=6,
        minimum_segment_samples=1,
    )
    assert predict_pav(artifact, 0.5) == 0.0
    assert artifact["gates"]["approved_rubric_refs"] == ["rubric_1:1"]
    assert artifact_bytes(artifact) == json.dumps(
        artifact, sort_keys=True, separators=(",", ":")
    ).encode()


def test_live_matrix_requires_consent_key_and_pre_call_budget(monkeypatch) -> None:
    calls = []

    def invoke(case, model_name, thinking_enabled):
        calls.append((case.case_id, model_name, thinking_enabled))
        candidate = DEEPSEEK_MODEL_CANDIDATES[(model_name, thinking_enabled)]
        return LiveCaseResult(
            case_id=case.case_id,
            candidate_id=candidate.candidate_id,
            provider_model=model_name,
            system_fingerprint="fp_test_1",
            estimated_cost_usd=0.1,
            status="succeeded",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
        )

    cases = [LiveEvaluationCase("case_1")]
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="not configured"):
        run_live_matrix(
            cases,
            invoke=invoke,
            estimate_cost=lambda *_: 0.1,
            live=True,
            acknowledge_third_party_processing=True,
            max_cases=1,
            budget_usd=1.0,
        )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-persist-this")
    with pytest.raises(ValueError, match="budget_usd"):
        run_live_matrix(
            cases,
            invoke=invoke,
            estimate_cost=lambda *_: 0.1,
            live=True,
            acknowledge_third_party_processing=True,
            max_cases=1,
            budget_usd=float("nan"),
        )
    guarded = run_live_matrix(
        cases,
        invoke=invoke,
        estimate_cost=lambda *_: 0.6,
        live=True,
        acknowledge_third_party_processing=True,
        max_cases=1,
        budget_usd=0.5,
    )
    assert guarded["stopped_reason"] == "pre_call_budget_guard"
    assert not calls
    complete = run_live_matrix(
        cases,
        invoke=invoke,
        estimate_cost=lambda *_: 0.1,
        live=True,
        acknowledge_third_party_processing=True,
        max_cases=1,
        budget_usd=1.0,
    )
    assert complete["result_count"] == 4
    assert "never-persist-this" not in json.dumps(complete)
