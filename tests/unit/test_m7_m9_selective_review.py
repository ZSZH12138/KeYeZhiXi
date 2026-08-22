from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone

import pytest

from course_insight.contracts.assessment import (
    CriterionScore,
    ItemInstance,
    RubricScoringResult,
    RubricScoringTask,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.evidence import (
    EvidenceBundle,
    EvidenceChunk,
    evidence_id_for_chunk,
)
from course_insight.contracts.knowledge import ReviewPolicy, Rubric, RubricCriterion
from course_insight.modules.m7_local_model.policy import (
    DEEPSEEK_MODEL_CANDIDATES,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.privacy import OutboundPrivacyResult
from course_insight.modules.m7_local_model.privacy_reviewer import PrivacyReviewResult
from course_insight.modules.m7_local_model.review_selection import (
    AllReviewSelector,
    ReviewSelectorBinding,
    build_review_selector,
    load_isotonic_review_selector,
)
from course_insight.modules.m7_local_model.runtime import (
    build_deepseek_m7_adapter,
    required_deepseek_api_key_env,
)
from course_insight.modules.m9_teacher_analytics.narrative_evaluation import (
    NarrativeEvaluationCase,
    evaluate_narrative_cases,
)
from course_insight.modules.m9_teacher_analytics.quality import (
    TechnicalQualityExpectation,
    evaluate_technical_quality_evidence,
    technical_quality_payload_checksum,
)
from course_insight.modules.m9_teacher_analytics.review_sampling import (
    ReviewSamplingPolicy,
    build_review_queue,
    decide_review_sampling,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
EVIDENCE_ID = evidence_id_for_chunk("chunk_1")


class _AllowPrivacyReviewer:
    reviewer_id = "test-allow-v1"

    def review(self, text: str) -> PrivacyReviewResult:
        del text
        return PrivacyReviewResult(
            decision="allow",
            reason_codes=("test",),
            reviewer_ids=(self.reviewer_id,),
        )


def _task() -> RubricScoringTask:
    rubric = Rubric(
        rubric_id="rubric_1",
        version="1",
        total_score=2.0,
        criteria=[
            RubricCriterion(
                criterion_id="criterion_1",
                description="Uses evidence",
                max_score=2.0,
                expected_student_evidence="quoted evidence",
                course_evidence_ids=[EVIDENCE_ID],
            )
        ],
        review_policy=ReviewPolicy(
            low_confidence_threshold=0.7,
            double_score_disagreement_threshold=1.0,
            require_evidence_for_positive_score=True,
        ),
        status="published",
    )
    return RubricScoringTask(
        scoring_task_id="scoring_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        item_instance=ItemInstance(
            item_instance_id="item_instance_1",
            item_id="item_1",
            item_version="1",
            stem="Explain.",
            parameters={},
            concept_ids=["concept_1"],
            rubric_id="rubric_1",
            max_score=2.0,
            source_evidence_ids=[EVIDENCE_ID],
        ),
        student_answer="The answer uses evidence.",
        rubric=rubric,
        evidence_query_id="query_1",
        created_at=NOW,
    )


def _evidence() -> EvidenceBundle:
    return EvidenceBundle(
        query_id="query_1",
        index_id="index_1",
        course_id="course_1",
        evidence_chunks=[
            EvidenceChunk(
                evidence_id=EVIDENCE_ID,
                source_id="source_1",
                chunk_id="chunk_1",
                text="Evidence text.",
                locator="paragraph:1",
                concept_ids=["concept_1"],
                relevance=0.9,
                checksum="a" * 64,
            )
        ],
        retrieved_at=NOW,
    )


def _result(*, confidence: float = 0.9) -> RubricScoringResult:
    return RubricScoringResult(
        scoring_task_id="scoring_1",
        criterion_scores=[
            CriterionScore(
                criterion_id="criterion_1",
                score=2.0,
                student_evidence="uses evidence",
                course_evidence_id=EVIDENCE_ID,
                reason="supported",
            )
        ],
        total_score=2.0,
        confidence=confidence,
        missing_concept_ids=[],
        review_flags=[],
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        scored_at=NOW,
    )


def _privacy(decision: str = "allowed") -> OutboundPrivacyResult:
    return OutboundPrivacyResult(
        decision=decision,  # type: ignore[arg-type]
        policy_version="m7-outbound-privacy-v2",
        input_checksum="a" * 64,
        outbound_checksum="b" * 64,
        flags=(),
        redaction_count=0 if decision == "allowed" else 1,
        outbound_text=("safe" if decision != "blocked" else None),
    )


def _binding() -> ReviewSelectorBinding:
    return ReviewSelectorBinding(
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        thinking_mode="non_thinking",
        prompt_id="m7-rubric-scoring-json",
        prompt_version="5.0.0",
        execution_policy_version="m7-governed-v2",
        privacy_policy_version="m7-outbound-privacy-v2",
        calibration_data_id="course_gold_1",
        calibration_data_version="1",
        calibration_data_sha256="c" * 64,
        split_id="question_split_1",
        split_sha256="d" * 64,
    )


def _artifact(binding: ReviewSelectorBinding) -> bytes:
    ranges = {
        "raw_risk": {"minimum": 0.0, "maximum": 1.0},
        "answer_characters": {"minimum": 0.0, "maximum": 1000.0},
        "criterion_count": {"minimum": 1.0, "maximum": 10.0},
        "evidence_chunk_count": {"minimum": 1.0, "maximum": 10.0},
        "evidence_characters": {"minimum": 0.0, "maximum": 10000.0},
        "normalized_total_score": {"minimum": 0.0, "maximum": 1.0},
        "missing_concept_fraction": {"minimum": 0.0, "maximum": 1.0},
        "cited_criterion_fraction": {"minimum": 0.0, "maximum": 1.0},
    }
    payload = {
        "schema_version": "m7-review-selector-artifact-v1",
        "selector_id": "selector_1",
        "selector_version": "1",
        "bindings": binding.to_dict(),
        "calibration": {
            "method": "pav_isotonic",
            "segments": [
                {
                    "raw_risk_upper": 1.0,
                    "calibrated_risk": 0.0,
                    "sample_count": 100,
                    "error_count": 0,
                }
            ],
        },
        "gates": {
            "minimum_calibration_samples": 100,
            "minimum_acceptance_samples": 100,
            "minimum_segment_samples": 20,
            "acceptance_raw_risk_upper": 1.0,
            "maximum_calibrated_risk": 0.05,
            "maximum_acceptance_error_rate": 0.05,
            "confidence_level": 0.95,
            "upper_bound_method": "wilson_one_sided",
            "approved_rubric_refs": ["rubric_1:1"],
            "ood_ranges": ranges,
        },
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_four_model_modes_and_runtime_never_accept_raw_key() -> None:
    assert len(DEEPSEEK_MODEL_CANDIDATES) == 4
    assert {
        (candidate.model_name, candidate.thinking_enabled)
        for candidate in DEEPSEEK_MODEL_CANDIDATES.values()
    } == {
        ("deepseek-v4-flash", False),
        ("deepseek-v4-flash", True),
        ("deepseek-v4-pro", False),
        ("deepseek-v4-pro", True),
    }
    assert "api_key" not in inspect.signature(build_deepseek_m7_adapter).parameters
    assert required_deepseek_api_key_env() == "DEEPSEEK_API_KEY"
    for model_name, thinking in DEEPSEEK_MODEL_CANDIDATES:
        policy = M7ExecutionPolicy(
            model_name=model_name,
            thinking_enabled=thinking,
        )
        adapter = build_deepseek_m7_adapter(
            policy=policy,
            privacy_reviewer=_AllowPrivacyReviewer(),
        )
        assert adapter._client.policy.model_name == model_name
        assert adapter._client.policy.thinking_enabled is thinking


def test_selector_modes_are_pinned_and_fail_closed(tmp_path) -> None:
    content = _artifact(_binding())
    path = tmp_path / "selector.json"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    selective = load_isotonic_review_selector(
        artifact_path=path,
        expected_artifact_sha256=digest,
        expected_binding=_binding(),
        mode="selective",
    )
    accepted = selective.select(
        task=_task(),
        evidence_bundle=_evidence(),
        result=_result(),
        privacy=_privacy(),
    )
    assert accepted.review_flags == ()
    assert accepted.candidate_accepted

    unseen_rubric_task = _task().model_copy(
        update={"rubric": _task().rubric.model_copy(update={"version": "2"})},
        deep=True,
    )
    unseen = selective.select(
        task=unseen_rubric_task,
        evidence_bundle=_evidence(),
        result=_result(),
        privacy=_privacy(),
    )
    assert unseen.require_teacher_review
    assert "review_selector_hard_defer" in unseen.review_flags

    mismatched_policy = M7ExecutionPolicy(
        model_name="deepseek-v4-pro",
        thinking_enabled=False,
        review_selection_mode="selective",
    )
    with pytest.raises(ValueError, match="selector binding"):
        build_deepseek_m7_adapter(
            policy=mismatched_policy,
            privacy_reviewer=_AllowPrivacyReviewer(),
            review_selector=selective,
        )

    shadow = load_isotonic_review_selector(
        artifact_path=path,
        expected_artifact_sha256=digest,
        expected_binding=_binding(),
        mode="shadow",
    ).select(
        task=_task(),
        evidence_bundle=_evidence(),
        result=_result(),
        privacy=_privacy(),
    )
    assert shadow.review_flags == (
        "teacher_review_required",
        "review_selector_shadow",
    )
    assert selective.select(
        task=_task(),
        evidence_bundle=_evidence(),
        result=_result(),
        privacy=_privacy("redacted"),
    ).require_teacher_review

    fallback = build_review_selector(
        mode="selective",
        expected_binding=_binding(),
        artifact_path=path,
        expected_artifact_sha256="f" * 64,
    )
    assert isinstance(fallback, AllReviewSelector)
    decision = fallback.select(
        task=_task(),
        evidence_bundle=_evidence(),
        result=_result(),
        privacy=_privacy(),
    )
    assert decision.require_teacher_review
    assert "review_selector_fallback" in decision.review_flags


def _audit(method: str, status: str, *, audit_id: str = "audit_1") -> ScoreAuditRecord:
    return ScoreAuditRecord(
        audit_id=audit_id,
        audit_version=1,
        attempt_id="attempt_1",
        item_instance_id=f"item_{audit_id}",
        criterion_scores=[],
        total_score=0.0,
        max_score=1.0,
        confidence=0.9,
        scoring_method=method,  # type: ignore[arg-type]
        review_status=status,
        review_reason=([] if status == "not_required" else ["required"]),
        created_at=NOW,
    )


def test_m9_sampling_is_latest_local_model_only_and_replayable() -> None:
    policy = ReviewSamplingPolicy(
        hmac_key_id="review-sample-key-v1",
        default_not_required_rate=1.0,
    )
    key = b"k" * 32
    local = _audit("local_model", "not_required")
    first = decide_review_sampling(
        local,
        learner_id="learner_1",
        policy=policy,
        hmac_key=key,
    )
    second = decide_review_sampling(
        local,
        learner_id="learner_1",
        policy=policy,
        hmac_key=key,
    )
    assert first == second
    assert first.disposition == "quality_audit_sample"
    assert decide_review_sampling(
        _audit("rule", "not_required"),
        learner_id="learner_1",
        policy=policy,
        hmac_key=key,
    ).disposition == "excluded_completed"
    assert decide_review_sampling(
        _audit("teacher_override", "confirmed"),
        learner_id="learner_1",
        policy=policy,
        hmac_key=key,
    ).disposition == "excluded_completed"
    assert decide_review_sampling(
        _audit("local_model", "rejected_pending_rescore"),
        learner_id="learner_1",
        policy=policy,
        hmac_key=key,
    ).disposition == "excluded_rejected"

    bundle = ScoringResultBundle.model_construct(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        score_audit_records=[local],
    )
    queue = build_review_queue(bundle, policy=policy, hmac_key=key)
    assert queue[0].review_reasons == ["quality_audit_sample"]


def test_quality_gate_is_checksum_bound_and_has_no_publish_side_effect(tmp_path) -> None:
    expectation = TechnicalQualityExpectation(
        subject_ref="m9_candidate_1",
        data_id="data_1",
        data_version="1",
        data_checksum="a" * 64,
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        mode="non_thinking",
        prompt_id="m9-teacher-interpretation-json",
        prompt_version="3.1.0",
        policy_version="m9-teacher-interpretation-v2",
        split_id="test_1",
        split_checksum="b" * 64,
    )
    payload = {
        "schema_version": "m9-technical-quality-evidence-v1",
        "evidence_id": "evidence_1",
        **{name: getattr(expectation, name) for name in (
            "subject_ref", "data_id", "data_version", "data_checksum",
            "model_name", "model_version", "mode", "prompt_id",
            "prompt_version", "policy_version", "split_id", "split_checksum",
        )},
        "observation_count": 100,
        "teacher_label_count": 30,
        "metrics": {
            "citation_precision": 1.0,
            "fact_fidelity_rate": 1.0,
            "schema_valid_rate": 1.0,
            "teacher_acceptance_rate": 0.9,
            "unsafe_output_rate": 0.0,
        },
        "generated_at": NOW.isoformat(),
    }
    payload["payload_checksum"] = technical_quality_payload_checksum(payload)
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "quality.json"
    path.write_bytes(content)
    report = evaluate_technical_quality_evidence(
        path,
        expected_file_sha256=hashlib.sha256(content).hexdigest(),
        expectation=expectation,
        requested_at=NOW,
    )
    assert report.status == "ready"
    assert report.subject_ref == "m9_candidate_1"
    with pytest.raises(Exception):
        evaluate_technical_quality_evidence(
            path,
            expected_file_sha256="f" * 64,
            expectation=expectation,
            requested_at=NOW,
        )


def test_narrative_candidate_stays_off_and_without_labels_is_insufficient() -> None:
    report = evaluate_narrative_cases(
        [
            NarrativeEvaluationCase(
                case_id="case_1",
                expected_fact_refs=("fact_1",),
                actual_fact_refs=("fact_1",),
                expected_question_codes=("check_1",),
                actual_question_codes=("check_1",),
                allowed_citation_ids=("fact_1",),
                actual_citation_ids=("fact_1",),
                schema_valid=True,
                meaning_reversal=False,
                unsafe_output=False,
            )
        ]
    )
    assert report["status"] == "insufficient_data"
    assert report["candidate_default_enabled"] is False
