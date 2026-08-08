from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from course_insight.contracts.analytics import (
    ClassReport,
    IndividualReport,
    ReviewQueueItem,
    TeacherAnalyticsBundle,
    TeachingSuggestion,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.platform import ActorContext
from course_insight.contracts.state import (
    ClassConceptStatus,
    ClassMisconceptionSummary,
    MasteryDistribution,
)
from course_insight.infrastructure.deepseek import (
    DeepSeekClient,
    DeepSeekHTTPResponse,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite import connect_sqlite, migrate
from course_insight.infrastructure.sqlite.m9_repository import (
    SQLiteM9Repository,
)
from course_insight.infrastructure.sqlite.module_recovery_schema import (
    M9_MODEL_INVOCATION_AUDITS_V14_SQL,
)
from course_insight.modules.m9_teacher_analytics.adapter import (
    DeepSeekM9NarrativeAdapter,
)
from course_insight.modules.m9_teacher_analytics.policy import (
    M9NarrativePolicy,
)
from course_insight.modules.m9_teacher_analytics.prompts import (
    NARRATIVE_PROMPT_VERSION,
    teacher_narrative_prompt,
)
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


class _Transport:
    def __init__(self, *responses: DeepSeekHTTPResponse) -> None:
        self.responses = list(responses)
        self.payloads: list[bytes] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        del url, headers, timeout_seconds, max_response_bytes
        self.payloads.append(payload)
        return self.responses.pop(0)


class _Repository:
    def __init__(self, analytics: TeacherAnalyticsBundle) -> None:
        self.analytics = analytics.model_copy(deep=True)
        self.course_id = "course_private_scope"
        self.audits: list[M9ModelAuditRecord] = []

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        if report_id != self.analytics.report_id:
            return None
        return self.analytics.model_copy(deep=True)

    def get_scoped_analytics(
        self,
        report_id: str,
        *,
        course_id: str,
        class_id: str,
    ) -> TeacherAnalyticsBundle | None:
        if (
            report_id != self.analytics.report_id
            or course_id != self.course_id
            or class_id != self.analytics.class_report.class_id
        ):
            return None
        return self.analytics.model_copy(deep=True)

    def save_model_audit(self, record: M9ModelAuditRecord) -> None:
        self.audits.append(record)


def _analytics(
    *,
    concept_sample_count: int = 8,
    misconception_evidence_attempts: int = 8,
) -> TeacherAnalyticsBundle:
    return TeacherAnalyticsBundle(
        report_id="report_private_scope",
        class_report=ClassReport(
            class_id="class_private_scope",
            coverage_rate=0.75,
            concept_summaries=[
                ClassConceptStatus(
                    concept_id="concept_private_scope",
                    mastery_distribution=MasteryDistribution(
                        mastered=0.2,
                        consolidating=0.2,
                        priority_support=0.6,
                    ),
                    mean_mastery_probability=0.4,
                    mean_confidence=0.7,
                    mastery_trend_delta=None,
                    trend_comparable=False,
                    sample_count=concept_sample_count,
                )
            ],
            misconception_summaries=[
                ClassMisconceptionSummary(
                    misconception_id="misconception_private_scope",
                    affected_count=3,
                    affected_rate_among_assessed=0.375,
                    evidence_attempts=misconception_evidence_attempts,
                )
            ],
            score_statistics={
                "audit_count": 8.0,
                "score_total": 16.0,
                "score_mean": 2.0,
            },
            evidence_status="sufficient",
        ),
        individual_reports=[
            IndividualReport(
                learner_id="learner_private_scope",
                overall_mastery=0.4,
                weak_concept_ids=["concept_private_scope"],
                active_misconception_ids=["misconception_private_scope"],
                recent_score=2.0,
                review_required_count=1,
            )
        ],
        review_queue=[
            ReviewQueueItem(
                audit_id="audit_private_scope",
                audit_version=1,
                learner_id="learner_private_scope",
                item_instance_id="item_private_scope",
                recommended_score=2.0,
                confidence=0.4,
                review_reasons=["teacher_review_required"],
            )
        ],
        teaching_suggestions=[
            TeachingSuggestion(
                suggestion_id="suggestion_private_scope",
                action_type="targeted_support",
                concept_ids=["concept_private_scope"],
                content=(
                    "Provide a targeted review using the assessed course concept."
                ),
                trigger_metrics={"priority_support_rate": 0.6},
                affected_count=5,
                affected_rate=0.625,
                coverage_rate=0.75,
                confidence=0.7,
                evidence_ids=["audit_private_scope"],
                status="candidate",
            )
        ],
        generated_at=NOW,
    )


def _teacher(
    *,
    course_ids: list[str] | None = None,
    class_ids: list[str] | None = None,
) -> ActorContext:
    return ActorContext(
        actor_id="pseudonym_teacher",
        role="teacher",
        course_ids=(
            ["course_private_scope"] if course_ids is None else course_ids
        ),
        class_ids=(
            ["class_private_scope"] if class_ids is None else class_ids
        ),
        issued_at=NOW,
    )


def _valid_output() -> dict[str, Any]:
    return {
        "schema_version": "m9_teacher_interpretation_v2",
        "source_digest": "",
        "fact_interpretations": [
            {
                "fact_ref": "fact_1",
                "meaning_code": "evidence_sufficient",
                "render_code": "evidence_sufficient",
            },
            {
                "fact_ref": "fact_2",
                "meaning_code": "concept_needs_attention",
                "render_code": "concept_needs_attention",
            },
            {
                "fact_ref": "fact_3",
                "meaning_code": "pattern_medium",
                "render_code": "pattern_medium",
            },
        ],
        "review_questions": [
            {
                "question_code": "check_recent_classroom_evidence",
                "fact_refs": ["fact_1", "fact_2", "fact_3"],
            }
        ],
        "suggestion_explanations": [
            {
                "suggestion_ref": "suggestion_1",
                "action_code": "targeted_support",
                "status_code": "candidate",
                "explanation_code": "targeted_support_rule_basis",
                "fact_refs": ["fact_2"],
            }
        ],
        "citation_ids": ["fact_1", "fact_2", "fact_3"],
    }


def _bound_output(analytics: TeacherAnalyticsBundle) -> dict[str, Any]:
    prompt = teacher_narrative_prompt(analytics, M9NarrativePolicy())
    data = _valid_output()
    data["source_digest"] = prompt.source_digest
    return data


def _response(structured: dict[str, Any]) -> DeepSeekHTTPResponse:
    return DeepSeekHTTPResponse(
        status_code=200,
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                structured,
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 80,
                    "completion_tokens": 40,
                },
            },
            ensure_ascii=False,
        ).encode(),
        headers={},
    )


def _service(
    analytics: TeacherAnalyticsBundle,
    response: DeepSeekHTTPResponse,
) -> tuple[M9TeacherAnalyticsService, _Repository, _Transport]:
    repository = _Repository(analytics)
    transport = _Transport(response)
    client = DeepSeekClient(
        max_tokens=1536,
        max_attempts=1,
        transport=transport,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    service = M9TeacherAnalyticsService(
        repository,  # type: ignore[arg-type]
        object(),
        object(),
    )
    service.configure_teacher_interpreter(
        DeepSeekM9NarrativeAdapter(client)
    )
    return service, repository, transport


def test_teacher_interpretation_rehydrates_only_after_strict_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics()
    service, repository, transport = _service(
        analytics,
        _response(_bound_output(analytics)),
    )

    result = service.interpret_teacher_analytics(_teacher(), analytics)

    assert result.status == "succeeded"
    assert result.structured_output["scope"] == "class_aggregate"
    assert result.structured_output["source_report_id"] == analytics.report_id
    assert result.structured_output["fact_interpretations"][1][
        "source_ids"
    ] == ["concept_private_scope"]
    assert result.structured_output["fact_interpretations"][1]["text"] == (
        "该聚合概念事实值得教师关注，不能据此推断个体情况。"
    )
    assert result.structured_output["review_questions"] == [
        {
            "question_code": "check_recent_classroom_evidence",
            "text": "这些程序事实与近期课堂活动中的证据是否一致？",
            "fact_refs": ["fact_1", "fact_2", "fact_3"],
            "source_ids": [
                "report_private_scope",
                "concept_private_scope",
                "misconception_private_scope",
            ],
        }
    ]
    assert result.structured_output["suggestion_explanations"] == [
        {
            "suggestion_id": "suggestion_private_scope",
            "action_type": "targeted_support",
            "status": "candidate",
            "canonical_content": (
                "Provide a targeted review using the assessed course concept."
            ),
            "explanation_code": "targeted_support_rule_basis",
            "explanation": (
                "该候选建议由确定性规则依据所列聚合事实触发，"
                "指向针对性支持；是否采用由教师决定。"
            ),
            "fact_refs": ["fact_2"],
            "source_ids": [
                "audit_private_scope",
                "concept_private_scope",
            ],
        }
    ]
    assert repository.audits[0].validation_status == "passed"
    assert repository.audits[0].validated_output == result.structured_output
    assert repository.audits[0].input_tokens == 80
    model_input = transport.payloads[0].decode()
    for private_value in (
        "learner_private_scope",
        "class_private_scope",
        "item_private_scope",
        "audit_private_scope",
        "suggestion_private_scope",
        "concept_private_scope",
        "misconception_private_scope",
        "private-test-key",
    ):
        assert private_value not in model_input


def test_prompt_contains_only_anonymous_aggregate_qualitative_facts() -> None:
    prompt = teacher_narrative_prompt(_analytics(), M9NarrativePolicy())
    system_message, user_message = prompt.messages
    model_input = user_message["content"]

    assert NARRATIVE_PROMPT_VERSION == "3.1.0"
    for private_value in (
        "learner_private_scope",
        "class_private_scope",
        "item_private_scope",
        "audit_private_scope",
        "suggestion_private_scope",
        "concept_private_scope",
        "misconception_private_scope",
        "score_mean",
        "existing_content",
    ):
        assert private_value not in model_input
    assert '"fact_ref":"fact_1"' in model_input
    assert '"meaning_code":"evidence_sufficient"' in model_input
    payload = json.loads(model_input)
    assert payload["allowed_review_question_bindings"] == [
        {
            "question_code": "check_recent_classroom_evidence",
            "fact_refs": ["fact_1", "fact_2", "fact_3"],
        },
        {
            "question_code": "check_assessment_coverage",
            "fact_refs": ["fact_1"],
        },
        {
            "question_code": "check_concept_transfer",
            "fact_refs": ["fact_2"],
        },
        {
            "question_code": "check_misconception_context",
            "fact_refs": ["fact_3"],
        },
    ]
    assert '"text"' not in model_input
    assert '"explanation"' not in model_input
    assert "只服务教师" in system_message["content"]
    assert prompt.evidence_ids == (
        "report_private_scope",
        "concept_private_scope",
        "misconception_private_scope",
        "audit_private_scope",
    )


def _m9_audit(
    analytics: TeacherAnalyticsBundle,
) -> M9ModelAuditRecord:
    return M9ModelAuditRecord(
        invocation_id="invocation_legacy_binding",
        request_id="request_legacy_binding",
        source_report_id=analytics.report_id,
        source_report_checksum=analytics.content_checksum(),
        scope="class_aggregate",
        prompt_template_id="m9-teacher-narrative-json",
        prompt_template_version="3.1.0",
        output_schema_version="1.0.0",
        policy_version="m9-governed-v1",
        input_checksum="a" * 64,
        source_digest="b" * 64,
        provider="deepseek",
        model_name="deepseek-v4-flash",
        provider_status="failed",
        validation_status="not_run",
        safety_flags=(),
        input_tokens=0,
        output_tokens=0,
        latency_ms=3,
        error_code="DEEPSEEK_NETWORK_ERROR",
        output_checksum=None,
        validated_output={},
        created_at=NOW,
    )


def test_reported_coverage_concept_binding_fails_closed_with_visible_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics()
    prompt = teacher_narrative_prompt(analytics, M9NarrativePolicy())
    model_payload = json.loads(prompt.messages[1]["content"])
    coverage = next(
        binding
        for binding in model_payload["allowed_review_question_bindings"]
        if binding["question_code"] == "check_assessment_coverage"
    )
    assert coverage["fact_refs"] == ["fact_1"]

    data = _bound_output(analytics)
    data["review_questions"] = [
        {
            "question_code": "check_assessment_coverage",
            "fact_refs": ["fact_2"],
        }
    ]
    service, repository, _ = _service(analytics, _response(data))

    with pytest.raises(DomainError) as raised:
        service.interpret_teacher_analytics(_teacher(), analytics)

    assert raised.value.code == "INVALID_MODEL_JSON"
    assert repository.audits[0].validation_status == "blocked"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(extra_field="not allowed"),
        lambda data: data["fact_interpretations"][0].update(
            text="模型不得生成展示文字"
        ),
        lambda data: data["fact_interpretations"][0].update(
            meaning_code="evidence_limited"
        ),
        lambda data: data["fact_interpretations"][0].update(
            render_code="evidence_limited"
        ),
        lambda data: data["fact_interpretations"][1].update(
            render_code="已经完全掌握，教师可以按此调整教学"
        ),
        lambda data: data["review_questions"][0].update(
            fact_refs=["outside"]
        ),
        lambda data: data["review_questions"][0].update(
            question_code="<b>adjust_teaching_now</b>"
        ),
        lambda data: data["review_questions"][0].update(
            question_code="check_assessment_coverage",
            fact_refs=["fact_2"],
        ),
        lambda data: data["suggestion_explanations"][0].update(
            status_code="approved"
        ),
        lambda data: data["suggestion_explanations"][0].update(
            explanation_code="observe_rule_basis"
        ),
        lambda data: data["suggestion_explanations"][0].update(
            text="教师必须立即采用"
        ),
        lambda data: data.update(citation_ids=[]),
    ],
)
def test_interpretation_blocks_structure_semantic_and_injection_violations(
    monkeypatch: pytest.MonkeyPatch,
    mutate,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics()
    data = _bound_output(analytics)
    mutate(data)
    service, repository, _ = _service(analytics, _response(data))

    with pytest.raises(DomainError) as raised:
        service.interpret_teacher_analytics(_teacher(), analytics)

    assert raised.value.code == "INVALID_MODEL_JSON"
    assert repository.audits[0].provider_status == "succeeded"
    assert repository.audits[0].validation_status == "blocked"
    assert repository.audits[0].validated_output == {}
    assert repository.audits[0].error_code == "INVALID_MODEL_OUTPUT"


def test_interpretation_fails_closed_without_api_key_and_records_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    analytics = _analytics()
    service, repository, transport = _service(
        analytics,
        _response(_bound_output(analytics)),
    )

    with pytest.raises(DomainError) as raised:
        service.interpret_teacher_analytics(_teacher(), analytics)

    assert raised.value.code == "MODEL_ADAPTER_UNCONFIGURED"
    assert not transport.payloads
    assert repository.audits[0].provider_status == "failed"
    assert repository.audits[0].validation_status == "not_run"
    assert repository.audits[0].error_code == "DEEPSEEK_API_KEY_MISSING"


def test_minimum_group_boundary_blocks_before_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics(
        concept_sample_count=4,
        misconception_evidence_attempts=4,
    )
    service, repository, transport = _service(
        analytics,
        _response(_valid_output()),
    )

    with pytest.raises(DomainError) as raised:
        service.interpret_teacher_analytics(_teacher(), analytics)

    assert raised.value.code == "REPORT_SCOPE_INVALID"
    assert raised.value.details == {
        "reason": "minimum_aggregate_size_not_met"
    }
    assert not transport.payloads
    assert not repository.audits


@pytest.mark.parametrize(
    "actor",
    [
        ActorContext(
            actor_id="pseudonym_student",
            role="student",
            course_ids=["course_private_scope"],
            class_ids=["class_private_scope"],
            issued_at=NOW,
        ),
        _teacher(class_ids=["another_class"]),
        _teacher(course_ids=["another_course"]),
    ],
)
def test_teacher_role_course_and_class_scope_are_enforced_before_network(
    monkeypatch: pytest.MonkeyPatch,
    actor: ActorContext,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics()
    service, repository, transport = _service(
        analytics,
        _response(_bound_output(analytics)),
    )

    with pytest.raises(DomainError) as raised:
        service.interpret_teacher_analytics(actor, analytics)

    assert raised.value.code == "TEACHER_INTERPRETATION_FORBIDDEN"
    assert not transport.payloads
    assert not repository.audits


def test_only_authoritative_persisted_report_can_be_interpreted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics()
    service, repository, transport = _service(
        analytics,
        _response(_bound_output(analytics)),
    )
    repository.analytics = analytics.model_copy(
        update={"report_id": "another_report"}
    )

    with pytest.raises(DomainError) as raised:
        service.interpret_teacher_analytics(_teacher(), analytics)

    assert raised.value.code == "TEACHER_INTERPRETATION_FORBIDDEN"
    assert not transport.payloads
    assert not repository.audits


def test_m9_public_constructor_stays_at_three_dependencies() -> None:
    parameters = inspect.signature(
        M9TeacherAnalyticsService.__init__
    ).parameters
    assert list(parameters) == [
        "self",
        "repository",
        "statistics_engine",
        "suggestion_rule_engine",
    ]


def test_m9_policy_is_frozen_and_rejects_retries_or_client_drift() -> None:
    policy = M9NarrativePolicy()
    with pytest.raises(FrozenInstanceError):
        policy.temperature = 0.5  # type: ignore[misc]
    with pytest.raises(ValueError, match="forbids automatic model retries"):
        M9NarrativePolicy(max_attempts=2)

    retrying_client = DeepSeekClient(max_tokens=1536, max_attempts=2)
    with pytest.raises(ValueError, match="frozen M9 policy"):
        DeepSeekM9NarrativeAdapter(retrying_client)

    oversized_client = DeepSeekClient(max_tokens=2048, max_attempts=1)
    with pytest.raises(ValueError, match="frozen M9 policy"):
        DeepSeekM9NarrativeAdapter(oversized_client)


def test_teacher_interpreter_configuration_is_explicit_and_one_time() -> None:
    analytics = _analytics()
    service, _, _ = _service(
        analytics,
        _response(_bound_output(analytics)),
    )
    second = DeepSeekM9NarrativeAdapter(
        DeepSeekClient(max_tokens=1536, max_attempts=1)
    )

    with pytest.raises(RuntimeError, match="already configured"):
        service.configure_teacher_interpreter(second)


def test_sqlite_persists_one_idempotent_sanitized_m9_call_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    analytics = _analytics()
    repository = SQLiteM9Repository(tmp_path / "m9.sqlite3")
    repository.initialize()
    repository.insert_or_get_analytics(
        analytics,
        course_id="course_private_scope",
    )
    assert repository.get_scoped_analytics(
        analytics.report_id,
        course_id="course_private_scope",
        class_id="class_private_scope",
    ) == analytics
    assert repository.get_scoped_analytics(
        analytics.report_id,
        course_id="another_course",
        class_id="class_private_scope",
    ) is None
    client = DeepSeekClient(
        max_tokens=1536,
        max_attempts=1,
        transport=_Transport(_response(_bound_output(analytics))),
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    service = M9TeacherAnalyticsService(repository, object(), object())
    service.configure_teacher_interpreter(
        DeepSeekM9NarrativeAdapter(client)
    )

    result = service.interpret_teacher_analytics(_teacher(), analytics)
    invocation_id = f"invocation_{result.request_id}"
    stored = repository.get_model_audit(invocation_id)

    assert stored is not None
    assert stored.validation_status == "passed"
    assert stored.validated_output == result.structured_output
    repository.save_model_audit(stored)
    connection = sqlite3.connect(tmp_path / "m9.sqlite3")
    try:
        raw_payload, source_report_checksum = connection.execute(
            """
            SELECT payload, source_report_checksum
            FROM m9_model_invocation_audits
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
    finally:
        connection.close()
    assert "private-test-key" not in raw_payload
    assert "learner_private_scope" not in raw_payload
    assert "messages" not in raw_payload
    assert "你是只服务教师" not in raw_payload
    assert source_report_checksum == analytics.content_checksum()

    with sqlite3.connect(tmp_path / "m9.sqlite3") as connection:
        connection.execute(
            """
            UPDATE m9_model_invocation_audits
            SET source_report_checksum = ?
            WHERE invocation_id = ?
            """,
            ("0" * 64, invocation_id),
        )
    with pytest.raises(RuntimeError, match="identity mismatch"):
        repository.get_model_audit(invocation_id)


def test_sqlite_v15_upgrade_rejects_bad_legacy_source_binding_atomically(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "m9_legacy.sqlite3"
    analytics = _analytics()
    repository = SQLiteM9Repository(database_path)
    repository.initialize()
    repository.insert_or_get_analytics(
        analytics,
        course_id="course_private_scope",
    )
    repository.save_model_audit(_m9_audit(analytics))

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM m9_model_invocation_audits"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload"]))
        payload["source_report_checksum"] = "0" * 64
        tampered_payload = dumps_json(payload)
        tampered_payload_checksum = hashlib.sha256(
            tampered_payload.encode("utf-8")
        ).hexdigest()
        connection.execute("DROP TABLE m9_model_invocation_audits")
        connection.execute(M9_MODEL_INVOCATION_AUDITS_V14_SQL)
        connection.execute(
            """
            INSERT INTO m9_model_invocation_audits(
                invocation_id,
                request_id,
                source_report_id,
                scope,
                provider,
                model_name,
                provider_status,
                validation_status,
                created_at,
                payload,
                payload_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["invocation_id"],
                row["request_id"],
                row["source_report_id"],
                row["scope"],
                row["provider"],
                row["model_name"],
                row["provider_status"],
                row["validation_status"],
                row["created_at"],
                tampered_payload,
                tampered_payload_checksum,
            ),
        )
        connection.execute("DROP TABLE m7_model_invocation_audits")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 15")

    with connect_sqlite(database_path) as connection:
        with pytest.raises(RuntimeError, match="source report checksum mismatch"):
            migrate(connection)
        ledger_version = int(
            connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        )
        audit_columns = {
            str(item["name"])
            for item in connection.execute(
                "PRAGMA table_info('m9_model_invocation_audits')"
            ).fetchall()
        }
        m7_table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'm7_model_invocation_audits'
            """
        ).fetchone()

    assert ledger_version == 14
    assert "source_report_checksum" not in audit_columns
    assert m7_table is None


def test_sqlite_current_schema_rejects_tampered_independent_source_checksum(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "m9_current_tampered.sqlite3"
    analytics = _analytics()
    repository = SQLiteM9Repository(database_path)
    repository.initialize()
    repository.insert_or_get_analytics(
        analytics,
        course_id="course_private_scope",
    )
    audit = _m9_audit(analytics)
    repository.save_model_audit(audit)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE m9_model_invocation_audits
            SET source_report_checksum = ?
            WHERE invocation_id = ?
            """,
            ("0" * 64, audit.invocation_id),
        )

    with connect_sqlite(database_path) as connection:
        with pytest.raises(RuntimeError, match="source report checksum mismatch"):
            migrate(connection)
        assert int(
            connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        ) == 16
        assert str(
            connection.execute(
                """
                SELECT source_report_checksum
                FROM m9_model_invocation_audits
                WHERE invocation_id = ?
                """,
                (audit.invocation_id,),
            ).fetchone()[0]
        ) == "0" * 64
