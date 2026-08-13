from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from course_insight.modules.m4_task_orchestration.intent import (
    IntentPrediction,
    IntentStatus,
)
from course_insight.modules.m4_task_orchestration.intent_identity import (
    build_intent_request_identity,
    input_checksum,
)
from course_insight.modules.m4_task_orchestration.intent_policy import (
    IntentDecisionOutcome,
    IntentPolicy,
)
from course_insight.modules.m4_task_orchestration.intent_rules import (
    match_high_precision_rules,
    match_legacy_rules,
    resolve_task_type_from_rules,
)


COMPLETE_QA_SCORES = {
    "qa": 0.89,
    "diagnostic": 0.68,
    "practice": 0.03,
    "correction": 0.02,
    "stage_assessment": 0.01,
}


def test_intent_prediction_is_frozen_and_rejects_unsupported_labels() -> None:
    prediction = IntentPrediction.accepted(
        "qa",
        COMPLETE_QA_SCORES,
        "fixture",
        "1",
    )

    with pytest.raises(FrozenInstanceError):
        prediction.label = "practice"  # type: ignore[misc]
    with pytest.raises(ValueError, match="supported"):
        IntentPrediction.accepted(
            "out_of_scope",
            COMPLETE_QA_SCORES,
            "fixture",
            "1",
        )
    with pytest.raises(ValueError, match="status"):
        IntentPrediction(
            label=None,
            scores=COMPLETE_QA_SCORES,
            confidence=0.89,
            margin=0.21,
            status="accepted",  # type: ignore[arg-type]
            adapter_id="fixture",
            adapter_version="1",
        )


def test_reason_codes_are_recursively_immutable_values() -> None:
    prediction_codes = ["outside_supported_scope"]
    outcome_codes = ["below_min_confidence"]
    prediction = IntentPrediction(
        label="qa",
        scores=COMPLETE_QA_SCORES,
        confidence=0.89,
        margin=0.21,
        status=IntentStatus.ACCEPTED,
        adapter_id="fixture",
        adapter_version="1",
        reason_codes=prediction_codes,  # type: ignore[arg-type]
    )
    outcome = IntentDecisionOutcome(
        label="qa",
        status=IntentStatus.ACCEPTED,
        reason_codes=outcome_codes,  # type: ignore[arg-type]
    )

    prediction_codes.append("changed_after_construction")
    outcome_codes.append("changed_after_construction")

    assert prediction.reason_codes == ("outside_supported_scope",)
    assert outcome.reason_codes == ("below_min_confidence",)
    assert isinstance(prediction.reason_codes, tuple)
    assert isinstance(outcome.reason_codes, tuple)
    with pytest.raises(AttributeError):
        prediction.reason_codes.append("mutate")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        outcome.reason_codes.append("mutate")  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "unsafe_reason_code",
    [
        "13800138000",
        "2026001234",
        "student_answer",
    ],
)
def test_reason_codes_reject_pii_and_unregistered_free_form_tokens(
    unsafe_reason_code: str,
) -> None:
    with pytest.raises(ValueError, match="registered"):
        IntentPrediction.accepted(
            "qa",
            COMPLETE_QA_SCORES,
            "fixture",
            "1",
            reason_codes=(unsafe_reason_code,),
        )


def test_prediction_scores_are_complete_validated_and_recursively_immutable() -> None:
    mutable_scores = dict(COMPLETE_QA_SCORES)
    prediction = IntentPrediction.accepted(
        "qa",
        mutable_scores,
        "fixture",
        "1",
    )
    mutable_scores["qa"] = 0.01

    assert dict(prediction.scores) == COMPLETE_QA_SCORES
    assert prediction.confidence == pytest.approx(0.89)
    assert prediction.margin == pytest.approx(0.21)
    with pytest.raises(TypeError):
        prediction.scores["qa"] = 0.01  # type: ignore[index]
    with pytest.raises(ValueError, match="exact"):
        IntentPrediction.accepted(
            "qa",
            {key: value for key, value in COMPLETE_QA_SCORES.items() if key != "qa"},
            "fixture",
            "1",
        )
    with pytest.raises(ValueError, match="top"):
        IntentPrediction(
            label="practice",
            scores=COMPLETE_QA_SCORES,
            confidence=0.89,
            margin=0.21,
            status=IntentStatus.ACCEPTED,
            adapter_id="fixture",
            adapter_version="1",
        )
    with pytest.raises(ValueError, match="non-accepted"):
        IntentPrediction(
            label="qa",
            scores=COMPLETE_QA_SCORES,
            confidence=0.89,
            margin=0.21,
            status=IntentStatus.ABSTAINED,
            adapter_id="fixture",
            adapter_version="1",
        )


def test_private_request_identity_separates_hint_and_normalized_text() -> None:
    base = dict(
        course_id="c1",
        class_id="cl1",
        learner_id="l1",
        session_id="s1",
        knowledge_bundle_id="kb1",
        course_package_id="cp1",
    )
    practice = build_intent_request_identity(
        **base,
        task_type_hint=" Practice ",
        student_text="  再做一道题  ",
    )
    diagnostic = build_intent_request_identity(
        **base,
        task_type_hint="diagnostic",
        student_text="再做一道题",
    )
    changed_text = build_intent_request_identity(
        **base,
        task_type_hint="practice",
        student_text="换一道题",
    )

    assert practice["task_type_hint"] == "practice"
    assert practice["input_checksum"] == input_checksum("再做一道题")
    assert practice != diagnostic
    assert practice != changed_text
    assert "再做一道题" not in repr(practice)
    with pytest.raises(TypeError):
        practice["course_id"] = "changed"  # type: ignore[index]


def test_nfkc_compatibility_text_has_one_identity_and_rule_result() -> None:
    assert input_checksum("ＷＨＹ　ＡＬＧＥＢＲＡ") == input_checksum("why algebra")
    base = dict(
        course_id="c1",
        class_id="cl1",
        learner_id="l1",
        session_id="s1",
        knowledge_bundle_id="kb1",
        course_package_id="cp1",
        student_text="same",
    )
    assert build_intent_request_identity(
        **base,
        task_type_hint="ＳＴＡＧＥ　ＡＳＳＥＳＳＭＥＮＴ",
    ) == build_intent_request_identity(
        **base,
        task_type_hint="stage assessment",
    )
    assert (
        match_high_precision_rules("ＰＲＡＣＴＩＣＥ　 fractions").resolved_label
        == "practice"
    )


def test_conflicting_rule_families_do_not_use_first_match_priority() -> None:
    match = match_high_precision_rules("请先给我诊断，再安排练习")

    assert match.labels == ("diagnostic", "practice")
    assert match.resolved_label is None
    assert match_legacy_rules("diagnosis and exercise").resolved_label is None
    assert resolve_task_type_from_rules("diagnostic exam") is None


@pytest.mark.parametrize("text", ["how photosynthesis works", "what is the weather?"])
def test_broad_english_question_tokens_are_not_high_precision(text: str) -> None:
    assert match_high_precision_rules(text).labels == ()


@pytest.mark.parametrize(
    ("kind", "scores", "confidence", "margin", "status"),
    [
        (
            "accepted",
            COMPLETE_QA_SCORES,
            None,
            None,
            "accepted",
        ),
        (
            "accepted",
            {
                **COMPLETE_QA_SCORES,
                "qa": 0.69,
                "diagnostic": 0.48,
            },
            None,
            None,
            "abstained",
        ),
        (
            "accepted",
            {
                **COMPLETE_QA_SCORES,
                "diagnostic": 0.80,
            },
            None,
            None,
            "abstained",
        ),
        (
            "out_of_scope",
            COMPLETE_QA_SCORES,
            0.92,
            0.30,
            "out_of_scope",
        ),
    ],
)
def test_policy_requires_confidence_and_margin(
    kind: str,
    scores: dict[str, float],
    confidence: float | None,
    margin: float | None,
    status: str,
) -> None:
    prediction = (
        IntentPrediction.accepted("qa", scores, "fixture", "1")
        if kind == "accepted"
        else IntentPrediction.out_of_scope(
            scores,
            confidence,
            margin,
            "fixture",
            "1",
        )
    )
    assert IntentPolicy(0.70, 0.10).accept(prediction).status == status


@pytest.mark.parametrize("status", [IntentStatus.UNAVAILABLE, IntentStatus.FAILED])
def test_policy_preserves_normal_no_score_adapter_status(
    status: IntentStatus,
) -> None:
    prediction = IntentPrediction(
        label=None,
        scores={
            "qa": 0.0,
            "diagnostic": 0.0,
            "practice": 0.0,
            "correction": 0.0,
            "stage_assessment": 0.0,
        },
        confidence=None,
        margin=None,
        status=status,
        adapter_id="fixture",
        adapter_version="1",
        reason_codes=(f"adapter_{status.value}",),
    )

    assert IntentPolicy(0.70, 0.10).accept(prediction).status is status
