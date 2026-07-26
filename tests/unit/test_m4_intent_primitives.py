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


def test_intent_prediction_is_frozen_and_rejects_unsupported_labels() -> None:
    prediction = IntentPrediction.accepted("qa", 0.89, 0.21, "fixture", "1")

    with pytest.raises(FrozenInstanceError):
        prediction.label = "practice"  # type: ignore[misc]
    with pytest.raises(ValueError, match="supported"):
        IntentPrediction.accepted("out_of_scope", 0.89, 0.21, "fixture", "1")
    with pytest.raises(ValueError, match="status"):
        IntentPrediction(
            label=None,
            confidence=0.89,
            margin=0.21,
            status="accepted",  # type: ignore[arg-type]
            adapter_id="fixture",
            adapter_version="1",
        )


def test_reason_codes_are_recursively_immutable_values() -> None:
    prediction_codes = ["adapter_reason"]
    outcome_codes = ["policy_reason"]
    prediction = IntentPrediction(
        label="qa",
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

    assert prediction.reason_codes == ("adapter_reason",)
    assert outcome.reason_codes == ("policy_reason",)
    assert isinstance(prediction.reason_codes, tuple)
    assert isinstance(outcome.reason_codes, tuple)
    with pytest.raises(AttributeError):
        prediction.reason_codes.append("mutate")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        outcome.reason_codes.append("mutate")  # type: ignore[attr-defined]


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


def test_conflicting_rule_families_do_not_use_first_match_priority() -> None:
    match = match_high_precision_rules("请先给我诊断，再安排练习")

    assert match.labels == ("diagnostic", "practice")
    assert match.resolved_label is None
    assert match_legacy_rules("diagnosis and exercise").resolved_label is None
    assert resolve_task_type_from_rules("diagnostic exam") is None


@pytest.mark.parametrize(
    ("prediction", "status"),
    [
        (IntentPrediction.accepted("qa", 0.89, 0.21, "fixture", "1"), "accepted"),
        (IntentPrediction.accepted("qa", 0.69, 0.21, "fixture", "1"), "abstained"),
        (IntentPrediction.accepted("qa", 0.89, 0.09, "fixture", "1"), "abstained"),
        (IntentPrediction.out_of_scope(0.92, 0.30, "fixture", "1"), "out_of_scope"),
    ],
)
def test_policy_requires_confidence_and_margin(
    prediction: IntentPrediction,
    status: str,
) -> None:
    assert IntentPolicy(0.70, 0.10).accept(prediction).status == status
