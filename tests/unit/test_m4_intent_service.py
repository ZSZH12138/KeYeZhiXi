from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m4_task_orchestration.intent import (
    IntentPrediction,
    IntentStatus,
)
from course_insight.modules.m4_task_orchestration.intent_policy import IntentPolicy
from course_insight.modules.m4_task_orchestration.intent_service import (
    M4IntentService,
    StoredIntentDecision,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)
RAW_PRIVATE_TEXT = "不要持久化这段学生原文"


def _scores_for(label: str, confidence: float, margin: float) -> dict[str, float]:
    scores = {
        "qa": 0.0,
        "diagnostic": 0.0,
        "practice": 0.0,
        "correction": 0.0,
        "stage_assessment": 0.0,
    }
    runner_up = "diagnostic" if label != "diagnostic" else "qa"
    return {
        **scores,
        label: confidence,
        runner_up: confidence - margin,
    }


def _accepted(
    label: str,
    confidence: float,
    margin: float,
    adapter_id: str,
    adapter_version: str,
    reason_codes: tuple[str, ...] = (),
) -> IntentPrediction:
    return IntentPrediction.accepted(
        label,
        _scores_for(label, confidence, margin),
        adapter_id,
        adapter_version,
        reason_codes,
    )


def _out_of_scope(
    confidence: float,
    margin: float,
    adapter_id: str,
    adapter_version: str,
    reason_codes: tuple[str, ...] = (),
) -> IntentPrediction:
    return IntentPrediction.out_of_scope(
        _scores_for("qa", 0.20, 0.10),
        confidence,
        margin,
        adapter_id,
        adapter_version,
        reason_codes,
    )


def _normal_nonaccepted(status: IntentStatus) -> IntentPrediction:
    if status is IntentStatus.ABSTAINED:
        return IntentPrediction(
            label=None,
            scores=_scores_for("qa", 0.60, 0.05),
            confidence=0.60,
            margin=0.05,
            status=status,
            adapter_id="fixture",
            adapter_version="1",
            reason_codes=("model_abstained",),
        )
    if status is IntentStatus.OUT_OF_SCOPE:
        return _out_of_scope(
            0.95,
            0.40,
            "fixture",
            "1",
            ("outside_supported_scope",),
        )
    return IntentPrediction(
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


def _canonical_key(identity: Mapping[str, str | None]) -> str:
    payload = json.dumps(
        dict(identity),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _request(
    student_text: str = "请安排练习",
    task_type_hint: str | None = None,
) -> dict[str, str | None]:
    return {
        "student_text": student_text,
        "task_type_hint": task_type_hint,
        "course_id": "course_1",
        "class_id": "class_1",
        "learner_id": "learner_1",
        "session_id": "session_1",
        "knowledge_bundle_id": "bundle_1",
        "course_package_id": "package_1",
    }


class InMemoryM4Repository:
    def __init__(self) -> None:
        self.intent_decisions: dict[str, StoredIntentDecision] = {}

    def get_intent_decision(
        self,
        request_key: str,
    ) -> StoredIntentDecision | None:
        return self.intent_decisions.get(request_key)

    def insert_or_get_intent_decision(
        self,
        decision: StoredIntentDecision,
    ) -> StoredIntentDecision:
        existing = self.intent_decisions.get(decision.request_key)
        if existing is not None:
            return existing
        self.intent_decisions = {
            **self.intent_decisions,
            decision.request_key: decision,
        }
        return decision


class RecordingAdapter:
    def __init__(
        self,
        prediction: object,
        *,
        adapter_id: str | None = None,
        adapter_version: str | None = None,
    ) -> None:
        self._prediction = prediction
        self._adapter_id = (
            prediction.adapter_id
            if adapter_id is None and isinstance(prediction, IntentPrediction)
            else (adapter_id or "fixture")
        )
        self._adapter_version = (
            prediction.adapter_version
            if adapter_version is None and isinstance(prediction, IntentPrediction)
            else (adapter_version or "1")
        )
        self.calls: tuple[str, ...] = ()

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    def predict(self, text: str) -> Any:
        self.calls = (*self.calls, text)
        return self._prediction


class RaisingAdapter(RecordingAdapter):
    def predict(self, text: str) -> IntentPrediction:
        self.calls = (*self.calls, text)
        raise RuntimeError(f"adapter failed while handling {RAW_PRIVATE_TEXT}")


def _make_intent_service(
    repository: InMemoryM4Repository,
    *,
    adapter: RecordingAdapter | None = None,
    mode: str = "rules",
    fallback_to_rules: bool = True,
    fail_closed: bool = True,
) -> M4IntentService:
    return M4IntentService(
        repository,
        _canonical_key,
        mode=mode,
        adapter=adapter,
        policy=IntentPolicy(
            min_confidence=0.70,
            min_margin=0.10,
            fallback_to_rules=fallback_to_rules,
            fail_closed=fail_closed,
        ),
        policy_version="policy-1",
    )


def _only_decision(repository: InMemoryM4Repository) -> StoredIntentDecision:
    assert len(repository.intent_decisions) == 1
    return next(iter(repository.intent_decisions.values()))


def _corrupt_prediction(**updates: object) -> IntentPrediction:
    prediction = _accepted(
        "qa",
        0.90,
        0.30,
        "fixture",
        "1",
    )
    for field_name, value in updates.items():
        object.__setattr__(prediction, field_name, value)
    return prediction


def _legacy_v1_integer_score_checksum(
    decision: StoredIntentDecision,
) -> str:
    payload = decision.canonical_payload()
    for field_name in ("confidence", "margin"):
        value = payload[field_name]
        if type(value) is float and value.is_integer():
            payload[field_name] = int(value)
    shadow = payload["shadow"]
    if type(shadow) is dict:
        for field_name in ("confidence", "margin"):
            value = shadow[field_name]
            if type(value) is float and value.is_integer():
                shadow[field_name] = int(value)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def test_persisted_decision_wins_before_new_hint_or_adapter_execution() -> None:
    repository = InMemoryM4Repository()
    adapter = RecordingAdapter(
        _accepted(
            "diagnostic",
            0.99,
            0.50,
            "fixture",
            "2",
        )
    )
    service = _make_intent_service(repository, adapter=adapter, mode="active")

    first = service.resolve(**_request(student_text="请安排练习"))
    second = service.resolve(**_request(student_text="  请安排练习  "))

    assert first == second == "practice"
    assert adapter.calls == ()
    assert len(repository.intent_decisions) == 1


def test_service_replays_legacy_v1_integer_score_checksum() -> None:
    repository = InMemoryM4Repository()
    adapter = RecordingAdapter(
        _accepted(
            "practice",
            1,
            1,
            "fixture",
            "1",
        )
    )
    service = _make_intent_service(repository, adapter=adapter, mode="active")
    request = _request(student_text="unmapped legacy replay request")
    assert service.resolve(**request) == "practice"
    current = _only_decision(repository)
    legacy_checksum = _legacy_v1_integer_score_checksum(current)
    assert legacy_checksum != current.payload_checksum
    repository.intent_decisions = {
        current.request_key: replace(
            current,
            payload_checksum=legacy_checksum,
        )
    }

    assert service.resolve(**request) == "practice"
    assert adapter.calls == ("unmapped legacy replay request",)


def test_same_context_different_hint_or_text_has_distinct_private_decision() -> None:
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, mode="rules")

    assert service.resolve(**_request("练习", "practice")) == "practice"
    assert service.resolve(**_request("诊断", "diagnostic")) == "diagnostic"

    assert len(repository.intent_decisions) == 2


def test_blank_and_absent_hint_share_one_unpoisoned_rule_decision() -> None:
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, mode="rules")

    blank_hint = service.resolve(**_request("请安排练习", "   "))
    absent_hint = service.resolve(**_request("请安排练习", None))

    assert blank_hint == absent_hint == "practice"
    assert len(repository.intent_decisions) == 1
    assert _only_decision(repository).decision_source == "high_precision_rule"


def test_high_and_legacy_same_label_keeps_high_precision_source() -> None:
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(repository).resolve(
        **_request("explain why?")
    )

    assert resolved == "qa"
    stored = _only_decision(repository)
    assert stored.decision_source == "high_precision_rule"
    assert stored.reason_codes == ()


def test_rules_cross_rule_family_conflict_refuses() -> None:
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(repository).resolve(
            **_request("explain assessment")
        )

    assert captured.value.code == "UNSUPPORTED_TASK"
    assert captured.value.recoverable is True
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.ABSTAINED
    assert stored.resolved_task_type is None
    assert "conflicting_high_precision_rules" in stored.reason_codes


def test_active_model_can_resolve_cross_rule_family_conflict() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "diagnostic",
            0.91,
            0.31,
            "fixture",
            "cross-conflict-v1",
        )
    )
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
    ).resolve(**_request("explain assessment"))

    assert resolved == "diagnostic"
    stored = _only_decision(repository)
    assert stored.decision_source == "active_model"
    assert stored.adapter_version == "cross-conflict-v1"
    assert adapter.calls == ("explain assessment",)


@pytest.mark.parametrize(
    ("adapter", "expected_status"),
    [
        (
            RecordingAdapter(
                _accepted(
                    "qa",
                    0.51,
                    0.02,
                    "fixture",
                    "low-confidence",
                )
            ),
            IntentStatus.ABSTAINED,
        ),
        (
            RecordingAdapter(
                _out_of_scope(
                    0.98,
                    0.40,
                    "fixture",
                    "out-of-scope",
                )
            ),
            IntentStatus.OUT_OF_SCOPE,
        ),
        (
            RaisingAdapter(
                _accepted(
                    "qa",
                    0.99,
                    0.50,
                    "fixture",
                    "raises",
                )
            ),
            IntentStatus.FAILED,
        ),
    ],
)
def test_active_failed_model_never_falls_back_after_cross_rule_conflict(
    adapter: RecordingAdapter,
    expected_status: IntentStatus,
) -> None:
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request("explain assessment"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    stored = _only_decision(repository)
    assert stored.decision_status is expected_status
    assert stored.resolved_task_type is None
    assert stored.decision_source == "refusal"
    assert "conflicting_high_precision_rules" in stored.reason_codes
    assert adapter.calls == ("explain assessment",)


def test_shadow_cross_rule_family_conflict_is_audited_but_refused() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "stage_assessment",
            0.93,
            0.33,
            "fixture",
            "shadow-conflict-v1",
        )
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="shadow",
        ).resolve(**_request("explain assessment"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.ABSTAINED
    assert stored.resolved_task_type is None
    assert stored.shadow_label == "stage_assessment"
    assert stored.shadow_status is IntentStatus.ACCEPTED
    assert stored.shadow_agrees is None
    assert adapter.calls == ("explain assessment",)


@pytest.mark.parametrize(
    ("student_text", "expected_label"),
    [
        ("explain this concept", "qa"),
        ("diagnostic check", "diagnostic"),
        ("practice fractions", "practice"),
        ("correction please", "correction"),
        ("stage assessment", "stage_assessment"),
    ],
)
def test_rules_mode_keeps_typical_task_family_compatibility(
    student_text: str,
    expected_label: str,
) -> None:
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(repository).resolve(
        **_request(student_text)
    )

    assert resolved == expected_label
    assert _only_decision(repository).decision_source == "high_precision_rule"


def test_shadow_prediction_is_audited_but_cannot_override_rule() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "diagnostic",
            0.99,
            0.50,
            "fixture",
            "7",
        )
    )
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, adapter=adapter, mode="shadow")

    assert service.resolve(**_request("请给我练习")) == "practice"

    stored = _only_decision(repository)
    assert stored.resolved_task_type == "practice"
    assert stored.decision_source == "high_precision_rule"
    assert stored.shadow_label == "diagnostic"
    assert stored.shadow_status is IntentStatus.ACCEPTED
    assert stored.shadow_agrees is False
    assert adapter.calls == ("请给我练习",)


def test_shadow_prediction_cannot_override_legal_hint() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "diagnostic",
            0.99,
            0.50,
            "fixture",
            "7",
        )
    )
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, adapter=adapter, mode="shadow")

    assert service.resolve(**_request("arbitrary", "correction")) == "correction"

    stored = _only_decision(repository)
    assert stored.decision_source == "legal_hint"
    assert stored.shadow_label == "diagnostic"
    assert stored.shadow_agrees is False


def test_shadow_prediction_records_agreement_with_legacy_fallback() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "practice",
            0.99,
            0.50,
            "fixture",
            "8",
        )
    )
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(
        repository,
        adapter=adapter,
        mode="shadow",
    ).resolve(**_request("请安排训练"))

    assert resolved == "practice"
    stored = _only_decision(repository)
    assert stored.decision_source == "legacy_rule"
    assert stored.shadow_label == "practice"
    assert stored.shadow_agrees is True


def test_active_low_confidence_and_conflicting_fallback_refuse() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "qa",
            0.51,
            0.02,
            "fixture",
            "1",
        )
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request("我要练习，也要诊断"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    assert captured.value.recoverable is True
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.ABSTAINED
    assert stored.resolved_task_type is None
    assert "below_min_confidence" in stored.reason_codes
    assert "below_min_margin" in stored.reason_codes


def test_rules_mode_never_calls_injected_adapter() -> None:
    adapter = RecordingAdapter(
        _accepted("qa", 0.99, 0.50, "fixture", "1")
    )
    service = _make_intent_service(
        InMemoryM4Repository(),
        adapter=adapter,
        mode="rules",
    )

    assert service.resolve(**_request("请给我练习")) == "practice"
    assert adapter.calls == ()


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_model_mode_rejects_non_adapter_wiring_at_construction(mode: str) -> None:
    with pytest.raises(ValueError, match="IntentAdapter"):
        M4IntentService(
            InMemoryM4Repository(),
            _canonical_key,
            mode=cast(Any, mode),
            adapter=cast(Any, object()),
            policy=IntentPolicy(min_confidence=0.70, min_margin=0.10),
            policy_version="policy-1",
        )


def test_blank_text_refuses_before_adapter_and_is_replayable() -> None:
    adapter = RecordingAdapter(
        _accepted("qa", 0.99, 0.50, "fixture", "1")
    )
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, adapter=adapter, mode="active")

    for text in ("   ", "\n\t"):
        with pytest.raises(DomainError) as captured:
            service.resolve(**_request(text))
        assert captured.value.code == "UNSUPPORTED_TASK"
        assert captured.value.recoverable is True

    assert adapter.calls == ()
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.INVALID
    assert stored.reason_codes == ("blank_student_text",)


def test_invalid_hint_refuses_before_adapter_and_is_persisted() -> None:
    adapter = RecordingAdapter(
        _accepted("qa", 0.99, 0.50, "fixture", "1")
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request("arbitrary", "not-supported"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    assert adapter.calls == ()
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.INVALID
    assert stored.reason_codes == ("unsupported_hint",)


def test_adapter_exception_becomes_private_auditable_replayable_refusal() -> None:
    adapter = RaisingAdapter(
        _accepted("qa", 0.99, 0.50, "fixture", "1")
    )
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, adapter=adapter, mode="active")

    for _ in range(2):
        with pytest.raises(DomainError) as captured:
            service.resolve(**_request(RAW_PRIVATE_TEXT))
        assert captured.value.code == "UNSUPPORTED_TASK"

    assert len(adapter.calls) == 1
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.FAILED
    assert "adapter_exception" in stored.reason_codes
    assert "no_supported_intent" in stored.reason_codes
    assert RAW_PRIVATE_TEXT not in repr(stored)
    assert RAW_PRIVATE_TEXT not in captured.value.message
    assert RAW_PRIVATE_TEXT not in str(captured.value.details)


def test_active_model_can_resolve_after_rules_abstain() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "correction",
            0.91,
            0.31,
            "fixture",
            "3",
        )
    )
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
    ).resolve(**_request("需要个性化帮助"))

    assert resolved == "correction"
    stored = _only_decision(repository)
    assert stored.decision_source == "active_model"
    assert stored.adapter_version == "3"
    assert stored.confidence == pytest.approx(0.91)
    assert stored.margin == pytest.approx(0.31)


def test_active_abstention_allows_one_unambiguous_legacy_fallback() -> None:
    adapter = RecordingAdapter(
        _accepted(
            "qa",
            0.50,
            0.02,
            "fixture",
            "4",
        )
    )
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
    ).resolve(**_request("请安排训练"))

    assert resolved == "practice"
    stored = _only_decision(repository)
    assert stored.decision_source == "legacy_rule"
    assert "below_min_confidence" in stored.reason_codes


def test_out_of_scope_prediction_uses_governed_legacy_fallback() -> None:
    adapter = RecordingAdapter(
        _out_of_scope(
            0.98,
            0.40,
            "fixture",
            "5",
            ("outside_supported_scope",),
        )
    )
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
    ).resolve(**_request("请安排训练"))

    assert resolved == "practice"
    stored = _only_decision(repository)
    assert stored.decision_source == "legacy_rule"
    assert stored.resolved_task_type == "practice"
    assert "outside_supported_scope" in stored.reason_codes


def test_fallback_policy_can_refuse_normal_nonaccepted_adapter_outcomes() -> None:
    adapter = RecordingAdapter(
        _accepted("qa", 0.50, 0.02, "fixture", "1")
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
            fallback_to_rules=False,
        ).resolve(**_request("请安排训练"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.ABSTAINED
    assert stored.decision_source == "refusal"


@pytest.mark.parametrize(
    "status_name",
    ["ABSTAINED", "OUT_OF_SCOPE", "UNAVAILABLE", "FAILED"],
)
@pytest.mark.parametrize("fallback_to_rules", [True, False])
def test_normal_adapter_outcomes_are_governed_only_by_fallback_policy(
    status_name: str,
    fallback_to_rules: bool,
) -> None:
    status = getattr(IntentStatus, status_name)
    adapter = RecordingAdapter(_normal_nonaccepted(status))
    repository = InMemoryM4Repository()
    service = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
        fallback_to_rules=fallback_to_rules,
        fail_closed=True,
    )

    if fallback_to_rules:
        assert service.resolve(**_request("请安排训练")) == "practice"
        assert _only_decision(repository).decision_source == "legacy_rule"
    else:
        with pytest.raises(DomainError):
            service.resolve(**_request("请安排训练"))
        assert _only_decision(repository).decision_source == "refusal"


def test_adapter_exception_is_failed_and_can_use_normal_legacy_fallback() -> None:
    adapter = RaisingAdapter(
        _accepted("qa", 0.90, 0.20, "fixture", "1")
    )
    repository = InMemoryM4Repository()

    assert _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
    ).resolve(**_request("请安排训练")) == "practice"

    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.ACCEPTED
    assert stored.decision_source == "legacy_rule"
    assert stored.reason_codes == ("adapter_exception",)


def test_fail_closed_rejects_malformed_prediction_before_legacy_fallback() -> None:
    adapter = RecordingAdapter(_corrupt_prediction(scores={"qa": 0.9}))
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
            fallback_to_rules=True,
            fail_closed=True,
        ).resolve(**_request("请安排训练"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.INVALID
    assert stored.reason_codes == ("malformed_prediction",)


def test_non_fail_closed_policy_can_fallback_after_malformed_prediction() -> None:
    adapter = RecordingAdapter(_corrupt_prediction(scores={"qa": 0.9}))
    repository = InMemoryM4Repository()

    resolved = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
        fallback_to_rules=True,
        fail_closed=False,
    ).resolve(**_request("请安排训练"))

    assert resolved == "practice"
    stored = _only_decision(repository)
    assert stored.decision_source == "legacy_rule"
    assert stored.reason_codes == ("malformed_prediction",)


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ({"adapter_id": r"C:\private\student.txt"}, "unsafe_prediction_metadata"),
        ({"adapter_version": "other-safe-version"}, "unsafe_prediction_metadata"),
        ({"reason_codes": ("student raw text",)}, "unsafe_prediction_metadata"),
        ({"reason_codes": ("13800138000",)}, "unsafe_prediction_metadata"),
    ],
)
def test_prediction_metadata_is_untrusted_and_never_leaks(
    corruption: dict[str, object],
    expected_reason: str,
) -> None:
    prediction = _corrupt_prediction(**corruption)
    adapter = RecordingAdapter(
        prediction,
        adapter_id="fixture",
        adapter_version="1",
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request("请安排训练"))

    stored = _only_decision(repository)
    assert stored.reason_codes == (expected_reason,)
    assert stored.adapter_id == "fixture"
    leaked_values = tuple(
        str(item)
        for value in corruption.values()
        for item in (value if isinstance(value, tuple) else (value,))
    )
    for leaked in leaked_values:
        assert leaked not in repr(stored)
        assert leaked not in str(captured.value.details)


def test_unsafe_outer_adapter_identity_is_replaced_and_never_persisted() -> None:
    unsafe_path = r"C:\private\student-model.joblib"
    adapter = RecordingAdapter(
        _accepted("practice", 0.90, 0.30, "fixture", "1"),
        adapter_id=unsafe_path,
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError):
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request("请安排训练"))

    stored = _only_decision(repository)
    assert stored.adapter_id == "invalid-adapter"
    assert stored.adapter_version == "unavailable"
    assert stored.reason_codes == ("unsafe_adapter_metadata",)
    assert unsafe_path not in repr(stored)


@pytest.mark.parametrize(
    "prediction",
    [
        object(),
        _corrupt_prediction(label="out_of_scope"),
        _corrupt_prediction(confidence=float("nan")),
        _corrupt_prediction(confidence=float("inf")),
        _corrupt_prediction(confidence=1.1),
        _corrupt_prediction(margin=-0.1),
        _corrupt_prediction(adapter_version=" "),
        _corrupt_prediction(status=IntentStatus.ABSTAINED, label="qa"),
    ],
)
def test_malformed_adapter_predictions_fail_closed_without_raw_text(
    prediction: object,
) -> None:
    adapter = RecordingAdapter(
        prediction,
        adapter_id="fixture",
        adapter_version="1",
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request(RAW_PRIVATE_TEXT))

    assert captured.value.code == "UNSUPPORTED_TASK"
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.INVALID
    assert stored.reason_codes == (
        "unsafe_prediction_metadata"
        if getattr(prediction, "adapter_version", None) == " "
        else "malformed_prediction",
    )
    assert stored.confidence is None
    assert stored.margin is None
    assert RAW_PRIVATE_TEXT not in repr(stored)


def test_exact_replay_keeps_first_adapter_version() -> None:
    repository = InMemoryM4Repository()
    first_adapter = RecordingAdapter(
        _accepted(
            "qa",
            0.91,
            0.31,
            "fixture",
            "v1",
        )
    )
    second_adapter = RecordingAdapter(
        _accepted(
            "diagnostic",
            0.99,
            0.50,
            "fixture",
            "v2",
        )
    )

    first = _make_intent_service(
        repository,
        adapter=first_adapter,
        mode="active",
    ).resolve(**_request("需要个性化帮助"))
    second = _make_intent_service(
        repository,
        adapter=second_adapter,
        mode="active",
    ).resolve(**_request("  需要个性化帮助  "))

    assert first == second == "qa"
    assert first_adapter.calls == ("需要个性化帮助",)
    assert second_adapter.calls == ()
    assert _only_decision(repository).adapter_version == "v1"


def test_nfkc_equivalent_text_replays_one_private_decision() -> None:
    repository = InMemoryM4Repository()
    adapter = RecordingAdapter(
        _accepted("qa", 0.91, 0.31, "fixture", "v1")
    )
    service = _make_intent_service(
        repository,
        adapter=adapter,
        mode="active",
    )

    assert service.resolve(**_request("ＮＥＥＤ　ＨＥＬＰ")) == "qa"
    assert service.resolve(**_request("need help")) == "qa"
    assert adapter.calls == ("need help",)
    assert len(repository.intent_decisions) == 1


def test_stored_decision_checksum_detects_field_tampering() -> None:
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository)
    assert service.resolve(**_request("练习")) == "practice"
    stored = _only_decision(repository)

    with pytest.raises(ValueError, match="payload checksum"):
        replace(
            stored,
            resolved_task_type="diagnostic",
            payload_checksum=stored.payload_checksum,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"adapter_id": r"C:\private\model.joblib"},
        {"adapter_version": "student raw text"},
        {"reason_codes": ("student raw text",)},
    ],
)
def test_stored_decision_rejects_unsafe_audit_metadata(
    updates: dict[str, object],
) -> None:
    repository = InMemoryM4Repository()
    assert _make_intent_service(repository).resolve(**_request("练习")) == "practice"
    stored = _only_decision(repository)

    with pytest.raises(ValueError, match="safe|reason"):
        replace(
            stored,
            **updates,
            payload_checksum=None,
            _generate_checksum=True,
        )


@pytest.mark.parametrize("decision_source", ["refusal", "invented_source"])
def test_accepted_decision_rejects_illegal_source_at_construction(
    decision_source: str,
) -> None:
    repository = InMemoryM4Repository()
    assert _make_intent_service(repository).resolve(**_request("练习")) == "practice"
    stored = _only_decision(repository)

    with pytest.raises(ValueError, match="decision source"):
        replace(
            stored,
            decision_source=decision_source,
            payload_checksum=None,
            _generate_checksum=True,
        )


def test_refusal_rejects_accepted_source_at_construction() -> None:
    repository = InMemoryM4Repository()
    with pytest.raises(DomainError):
        _make_intent_service(repository).resolve(**_request(RAW_PRIVATE_TEXT))
    stored = _only_decision(repository)

    with pytest.raises(ValueError, match="decision source"):
        replace(
            stored,
            decision_source="legacy_rule",
            payload_checksum=None,
            _generate_checksum=True,
        )


def test_replay_rejects_checksum_valid_semantically_illegal_source() -> None:
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository)
    assert service.resolve(**_request("练习")) == "practice"
    poisoned = _only_decision(repository)
    object.__setattr__(poisoned, "decision_source", "refusal")
    object.__setattr__(
        poisoned,
        "payload_checksum",
        poisoned.recalculate_payload_checksum(),
    )

    with pytest.raises(RuntimeError, match="corrupt"):
        service.resolve(**_request("练习"))


def test_equivalent_created_at_offsets_have_one_utc_canonical_checksum() -> None:
    repository = InMemoryM4Repository()
    assert _make_intent_service(repository).resolve(**_request("练习")) == "practice"
    stored = _only_decision(repository)
    offset_created_at = stored.created_at.astimezone(
        timezone(timedelta(hours=8))
    )

    equivalent = replace(
        stored,
        created_at=offset_created_at,
        payload_checksum=None,
        _generate_checksum=True,
    )

    assert equivalent.created_at.tzinfo is timezone.utc
    assert equivalent.created_at == stored.created_at
    assert equivalent.payload_checksum == stored.payload_checksum


def test_stored_decision_normalizes_integer_scores_before_checksum() -> None:
    integer_scores = StoredIntentDecision(
        request_key="a" * 64,
        resolved_task_type="practice",
        decision_status=IntentStatus.ACCEPTED,
        decision_source="active_model",
        adapter_id="adapter",
        adapter_version="1",
        policy_version="policy-1",
        confidence=1,
        margin=0,
        input_checksum="b" * 64,
        reason_codes=("model_accepted",),
        created_at=NOW,
        shadow_label="qa",
        shadow_status=IntentStatus.ACCEPTED,
        shadow_adapter_id="shadow-adapter",
        shadow_adapter_version="1",
        shadow_confidence=1,
        shadow_margin=0,
        shadow_reason_codes=("shadow_accepted",),
        shadow_agrees=False,
        _generate_checksum=True,
    )
    float_scores = StoredIntentDecision(
        request_key="a" * 64,
        resolved_task_type="practice",
        decision_status=IntentStatus.ACCEPTED,
        decision_source="active_model",
        adapter_id="adapter",
        adapter_version="1",
        policy_version="policy-1",
        confidence=1.0,
        margin=0.0,
        input_checksum="b" * 64,
        reason_codes=("model_accepted",),
        created_at=NOW,
        shadow_label="qa",
        shadow_status=IntentStatus.ACCEPTED,
        shadow_adapter_id="shadow-adapter",
        shadow_adapter_version="1",
        shadow_confidence=1.0,
        shadow_margin=0.0,
        shadow_reason_codes=("shadow_accepted",),
        shadow_agrees=False,
        _generate_checksum=True,
    )

    assert type(integer_scores.confidence) is float
    assert type(integer_scores.margin) is float
    assert type(integer_scores.shadow_confidence) is float
    assert type(integer_scores.shadow_margin) is float
    assert integer_scores.canonical_payload() == float_scores.canonical_payload()
    assert integer_scores.payload_checksum == float_scores.payload_checksum


def test_stored_decision_accepts_only_exact_legacy_v1_score_checksum() -> None:
    current = StoredIntentDecision(
        request_key="a" * 64,
        resolved_task_type="practice",
        decision_status=IntentStatus.ACCEPTED,
        decision_source="active_model",
        adapter_id="adapter",
        adapter_version="1",
        policy_version="policy-1",
        confidence=1.0,
        margin=0.0,
        input_checksum="b" * 64,
        reason_codes=("model_accepted",),
        created_at=NOW,
        shadow_label="qa",
        shadow_status=IntentStatus.ACCEPTED,
        shadow_adapter_id="shadow-adapter",
        shadow_adapter_version="1",
        shadow_confidence=1.0,
        shadow_margin=0.0,
        shadow_reason_codes=("shadow_accepted",),
        shadow_agrees=False,
        _generate_checksum=True,
    )
    legacy_checksum = _legacy_v1_integer_score_checksum(current)

    replayed = replace(current, payload_checksum=legacy_checksum)

    assert replayed.payload_checksum == legacy_checksum
    assert replayed.payload_checksum != current.payload_checksum
    assert type(replayed.confidence) is float
    assert type(replayed.shadow_confidence) is float
    current.assert_integrity()
    with pytest.raises(ValueError, match="payload checksum"):
        replayed.assert_integrity()
    replayed.assert_persisted_integrity()
    with pytest.raises(ValueError, match="payload checksum"):
        replace(
            replayed,
            adapter_version="tampered",
            payload_checksum=legacy_checksum,
        )


def test_persisted_decision_cannot_omit_payload_checksum() -> None:
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository)
    assert service.resolve(**_request("练习")) == "practice"
    stored = _only_decision(repository)

    with pytest.raises(ValueError, match="payload checksum"):
        replace(stored, payload_checksum=None)


def test_stored_decision_contains_checksums_not_student_text() -> None:
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, mode="rules")

    with pytest.raises(DomainError):
        service.resolve(**_request(RAW_PRIVATE_TEXT))

    stored = _only_decision(repository)
    assert len(stored.request_key) == 64
    assert len(stored.input_checksum) == 64
    assert len(stored.payload_checksum) == 64
    assert stored.schema_version == 1
    assert stored.created_at.tzinfo is not None
    assert RAW_PRIVATE_TEXT not in repr(stored)
