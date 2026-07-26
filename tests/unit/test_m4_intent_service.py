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
        adapter_id: str = "fixture",
        adapter_version: str = "1",
    ) -> None:
        self._prediction = prediction
        self._adapter_id = adapter_id
        self._adapter_version = adapter_version
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
) -> M4IntentService:
    return M4IntentService(
        repository,
        _canonical_key,
        mode=mode,
        adapter=adapter,
        policy=IntentPolicy(min_confidence=0.70, min_margin=0.10),
        policy_version="policy-1",
    )


def _only_decision(repository: InMemoryM4Repository) -> StoredIntentDecision:
    assert len(repository.intent_decisions) == 1
    return next(iter(repository.intent_decisions.values()))


def _corrupt_prediction(**updates: object) -> IntentPrediction:
    prediction = IntentPrediction.accepted(
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
        IntentPrediction.accepted(
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


def test_shadow_prediction_is_audited_but_cannot_override_rule() -> None:
    adapter = RecordingAdapter(
        IntentPrediction.accepted(
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
        IntentPrediction.accepted(
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
        IntentPrediction.accepted(
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
        IntentPrediction.accepted(
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
        IntentPrediction.accepted("qa", 0.99, 0.50, "fixture", "1")
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
        IntentPrediction.accepted("qa", 0.99, 0.50, "fixture", "1")
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
        IntentPrediction.accepted("qa", 0.99, 0.50, "fixture", "1")
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
        IntentPrediction.accepted("qa", 0.99, 0.50, "fixture", "1")
    )
    repository = InMemoryM4Repository()
    service = _make_intent_service(repository, adapter=adapter, mode="active")

    for _ in range(2):
        with pytest.raises(DomainError) as captured:
            service.resolve(**_request(RAW_PRIVATE_TEXT))
        assert captured.value.code == "UNSUPPORTED_TASK"

    assert len(adapter.calls) == 1
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.ABSTAINED
    assert "adapter_exception" in stored.reason_codes
    assert "no_supported_intent" in stored.reason_codes
    assert RAW_PRIVATE_TEXT not in repr(stored)
    assert RAW_PRIVATE_TEXT not in captured.value.message
    assert RAW_PRIVATE_TEXT not in str(captured.value.details)


def test_active_model_can_resolve_after_rules_abstain() -> None:
    adapter = RecordingAdapter(
        IntentPrediction.accepted(
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
        IntentPrediction.accepted(
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


def test_out_of_scope_prediction_refuses_even_when_legacy_rule_matches() -> None:
    adapter = RecordingAdapter(
        IntentPrediction.out_of_scope(
            0.98,
            0.40,
            "fixture",
            "5",
            ("outside_supported_scope",),
        )
    )
    repository = InMemoryM4Repository()

    with pytest.raises(DomainError) as captured:
        _make_intent_service(
            repository,
            adapter=adapter,
            mode="active",
        ).resolve(**_request("请安排训练"))

    assert captured.value.code == "UNSUPPORTED_TASK"
    stored = _only_decision(repository)
    assert stored.decision_status is IntentStatus.OUT_OF_SCOPE
    assert stored.resolved_task_type is None


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
    adapter = RecordingAdapter(prediction)
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
    assert "malformed_prediction" in stored.reason_codes
    assert "no_supported_intent" in stored.reason_codes
    assert stored.confidence is None
    assert stored.margin is None
    assert RAW_PRIVATE_TEXT not in repr(stored)


def test_exact_replay_keeps_first_adapter_version() -> None:
    repository = InMemoryM4Repository()
    first_adapter = RecordingAdapter(
        IntentPrediction.accepted(
            "qa",
            0.91,
            0.31,
            "fixture",
            "v1",
        )
    )
    second_adapter = RecordingAdapter(
        IntentPrediction.accepted(
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
        reason_codes=("accepted",),
        created_at=NOW,
        shadow_label="qa",
        shadow_status=IntentStatus.ACCEPTED,
        shadow_adapter_id="shadow-adapter",
        shadow_adapter_version="1",
        shadow_confidence=1,
        shadow_margin=0,
        shadow_reason_codes=("shadow-accepted",),
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
        reason_codes=("accepted",),
        created_at=NOW,
        shadow_label="qa",
        shadow_status=IntentStatus.ACCEPTED,
        shadow_adapter_id="shadow-adapter",
        shadow_adapter_version="1",
        shadow_confidence=1.0,
        shadow_margin=0.0,
        shadow_reason_codes=("shadow-accepted",),
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
        reason_codes=("accepted",),
        created_at=NOW,
        shadow_label="qa",
        shadow_status=IntentStatus.ACCEPTED,
        shadow_adapter_id="shadow-adapter",
        shadow_adapter_version="1",
        shadow_confidence=1.0,
        shadow_margin=0.0,
        shadow_reason_codes=("shadow-accepted",),
        shadow_agrees=False,
        _generate_checksum=True,
    )
    legacy_checksum = _legacy_v1_integer_score_checksum(current)

    replayed = replace(current, payload_checksum=legacy_checksum)

    assert replayed.payload_checksum == legacy_checksum
    assert replayed.payload_checksum != current.payload_checksum
    assert type(replayed.confidence) is float
    assert type(replayed.shadow_confidence) is float
    replayed.assert_integrity()
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
