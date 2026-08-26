from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.infrastructure.deepseek import DeepSeekClient, DeepSeekHTTPResponse
from course_insight.modules.m7_local_model.privacy_reviewer import PrivacyReviewResult
from course_insight.modules.m7_local_model.student_qa import (
    DeepSeekStudentQAAdapter,
    StudentQAConcept,
    StudentQAContext,
    StudentQAEvidence,
    StudentQAExample,
)


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


class _AllowReviewer:
    reviewer_id = "test-allow-reviewer"

    def review(self, text: str) -> PrivacyReviewResult:
        del text
        return PrivacyReviewResult(
            decision="allow",
            reason_codes=("test_allowed",),
            reviewer_ids=(self.reviewer_id,),
        )


class _Transport:
    def __init__(self, *structured: object) -> None:
        self.structured = list(structured)
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        del headers, timeout_seconds, max_response_bytes
        call = json.loads(payload)
        call["url"] = url
        self.calls.append(call)
        response = self.structured.pop(0)
        if isinstance(response, DeepSeekHTTPResponse):
            return response
        content = json.dumps(response, ensure_ascii=False)
        return DeepSeekHTTPResponse(
            200,
            json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": content},
                        }
                    ]
                }
            ).encode(),
            {},
        )


class _SchemaSensitiveClassifierTransport:
    """Mirror the provider behavior observed when the schema is only prose."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        del headers, timeout_seconds, max_response_bytes
        call = json.loads(payload)
        call["url"] = url
        self.calls.append(call)
        if len(self.calls) == 1:
            user_payload = json.loads(call["messages"][1]["content"])
            required_output = user_payload.get("required_output")
            system_prompt = call["messages"][0]["content"]
            if (
                isinstance(required_output, dict)
                and set(required_output)
                == {"matches", "coverage", "uncovered_parts", "citation_ids"}
                and "exactly matches, coverage, uncovered_parts, and citation_ids"
                in system_prompt
            ):
                structured = {
                    "matches": [
                        {"concept_id": "concept-1", "confidence": 0.93}
                    ],
                    "coverage": "full",
                    "uncovered_parts": [],
                    "citation_ids": [],
                }
            else:
                structured = {
                    "instruction": "Choose relevant concepts.",
                    "matches": [
                        {"concept_id": "concept-1", "confidence": 0.93}
                    ],
                }
        else:
            structured = {
                "analysis": "拥塞控制用于避免网络过载。",
                "citation_ids": ["evidence-1"],
            }
        return DeepSeekHTTPResponse(
            200,
            json.dumps(
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
                    ]
                },
                ensure_ascii=False,
            ).encode(),
            {},
        )


class _SchemaSensitiveAnalysisTransport:
    """Mirror the provider echo observed without an explicit analysis schema."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        del headers, timeout_seconds, max_response_bytes
        call = json.loads(payload)
        call["url"] = url
        self.calls.append(call)
        if len(self.calls) == 1:
            structured = {
                "matches": [
                    {"concept_id": "concept-1", "confidence": 0.93}
                ],
                "coverage": "full",
                "uncovered_parts": [],
                "citation_ids": [],
            }
        else:
            user_payload = json.loads(call["messages"][1]["content"])
            required_output = user_payload.get("required_output")
            system_prompt = call["messages"][0]["content"]
            if (
                isinstance(required_output, dict)
                and set(required_output) == {"analysis", "citation_ids"}
                and "exactly analysis and citation_ids" in system_prompt
            ):
                structured = {
                    "analysis": "拥塞控制用于避免网络过载。",
                    "citation_ids": ["evidence-1"],
                }
            else:
                structured = user_payload
        return DeepSeekHTTPResponse(
            200,
            json.dumps(
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
                    ]
                },
                ensure_ascii=False,
            ).encode(),
            {},
        )


def _concepts() -> tuple[StudentQAConcept, ...]:
    return (
        StudentQAConcept("concept-1", "拥塞控制", ("网络拥塞",)),
        StudentQAConcept("concept-2", "慢启动", ()),
    )


def _context(concept_id: str) -> StudentQAContext:
    if concept_id == "concept-2":
        return StudentQAContext(
            evidence=(
                StudentQAEvidence(
                    evidence_id="evidence-2",
                    source_version_id="version-2",
                    file_name="slow-start.txt",
                    locator="paragraph:2",
                    text="慢启动会逐步增加拥塞窗口。",
                ),
            ),
            examples=(
                StudentQAExample(
                    question_id="q-2",
                    stem="慢启动如何改变拥塞窗口？",
                    answer="逐步增加拥塞窗口。",
                ),
            ),
        )
    assert concept_id == "concept-1"
    return StudentQAContext(
        evidence=(
            StudentQAEvidence(
                evidence_id="evidence-1",
                source_version_id="version-1",
                file_name="chapter.txt",
                locator="paragraph:1",
                text="拥塞控制用于避免网络过载。",
            ),
        ),
        examples=(
            StudentQAExample(
                question_id="q-1",
                stem="拥塞控制的作用是什么？",
                answer="避免网络过载。",
            ),
        ),
    )


def _model_ref() -> LLMModelRef:
    return LLMModelRef(
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        status="configured",
    )


def _adapter(*outputs: object, web_search: bool = False):
    transport = _Transport(*outputs)
    client = DeepSeekClient(
        api_key="teacher-scoped-key",
        transport=transport,
        max_attempts=1,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    return (
        DeepSeekStudentQAAdapter(
            client,
            privacy_reviewer=_AllowReviewer(),
            **({"web_search_client": client} if web_search else {}),
        ),
        transport,
    )


def _web_response(
    analysis: str = "外部资料说明慢启动会逐步增加拥塞窗口。",
) -> DeepSeekHTTPResponse:
    return DeepSeekHTTPResponse(
        200,
        json.dumps(
            {
                "id": "response-qa-web",
                "object": "response",
                "status": "completed",
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "search-qa",
                        "status": "completed",
                        "action": {"type": "search", "query": "慢启动"},
                    },
                    {
                        "type": "message",
                        "id": "message-qa",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": analysis,
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "RFC 5681",
                                        "url": "https://www.rfc-editor.org/rfc/rfc5681",
                                        "start_index": 0,
                                        "end_index": 4,
                                    }
                                ],
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 12, "output_tokens": 8},
                "error": None,
                "incomplete_details": None,
            },
            ensure_ascii=False,
        ).encode(),
        {},
    )


def test_two_stage_answer_selects_multiple_concepts_before_loading_evidence() -> None:
    adapter, transport = _adapter(
        {
            "matches": [
                {"concept_id": "concept-1", "confidence": 0.93},
                {"concept_id": "concept-2", "confidence": 0.88},
            ],
            "coverage": "full",
            "uncovered_parts": [],
            "citation_ids": [],
        },
        {
            "analysis": "原文说明慢启动逐步增大窗口，并由拥塞控制避免网络过载。",
            "citation_ids": ["evidence-1", "evidence-2"],
        },
    )
    loaded: list[str] = []

    answer = adapter.answer(
        "拥塞控制有什么作用？",
        _concepts(),
        lambda concept_id: loaded.append(concept_id) or _context(concept_id),
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert loaded == ["concept-1", "concept-2"]
    assert len(transport.calls) == 2
    assert answer.concept_id == "concept-1"
    assert answer.concept_name == "拥塞控制、慢启动"
    assert [item.concept_id for item in answer.matched_concepts] == [
        "concept-1",
        "concept-2",
    ]
    assert answer.coverage == "full"
    assert answer.sources[0].file_name == "chapter.txt"
    assert answer.sources[1].file_name == "slow-start.txt"
    assert answer.analysis == "原文说明慢启动逐步增大窗口，并由拥塞控制避免网络过载。"
    assert [item.question_id for item in answer.examples] == ["q-1", "q-2"]
    second_messages = transport.calls[1]["messages"]
    assert "chapter.txt" in json.dumps(second_messages, ensure_ascii=False)
    assert "避免网络过载。" in json.dumps(second_messages, ensure_ascii=False)
    assert "slow-start.txt" in json.dumps(second_messages, ensure_ascii=False)


def test_classifier_supplies_exact_json_contract_for_large_catalogs() -> None:
    transport = _SchemaSensitiveClassifierTransport()
    client = DeepSeekClient(
        api_key="teacher-scoped-key",
        transport=transport,
        max_attempts=1,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    adapter = DeepSeekStudentQAAdapter(
        client,
        privacy_reviewer=_AllowReviewer(),
    )

    answer = adapter.answer(
        "拥塞控制有什么作用？",
        _concepts(),
        _context,
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert answer.status == "answered"
    assert answer.coverage == "full"
    assert [item.concept_id for item in answer.matched_concepts] == ["concept-1"]
    assert len(transport.calls) == 2


def test_analysis_supplies_exact_json_contract_instead_of_echoable_input() -> None:
    transport = _SchemaSensitiveAnalysisTransport()
    client = DeepSeekClient(
        api_key="teacher-scoped-key",
        transport=transport,
        max_attempts=1,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    adapter = DeepSeekStudentQAAdapter(
        client,
        privacy_reviewer=_AllowReviewer(),
    )

    answer = adapter.answer(
        "拥塞控制有什么作用？",
        _concepts(),
        _context,
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert answer.status == "answered"
    assert answer.analysis == "拥塞控制用于避免网络过载。"
    assert [source.evidence_id for source in answer.sources] == ["evidence-1"]
    assert len(transport.calls) == 2


def test_unknown_concept_id_is_rejected_before_evidence_lookup() -> None:
    adapter, transport = _adapter(
        {
            "matches": [{"concept_id": "invented", "confidence": 0.99}],
            "coverage": "full",
            "uncovered_parts": [],
            "citation_ids": [],
        }
    )

    with pytest.raises(DomainError, match="concept"):
        adapter.answer(
            "问题",
            _concepts(),
            _context,
            model_ref=_model_ref(),
            created_at=NOW,
        )

    assert len(transport.calls) == 1


def test_examples_and_source_quotes_are_server_owned_not_model_rewritten() -> None:
    adapter, _ = _adapter(
        {
            "matches": [{"concept_id": "concept-1", "confidence": 0.9}],
            "coverage": "full",
            "uncovered_parts": [],
            "citation_ids": [],
        },
        {"analysis": "模型分析", "citation_ids": ["evidence-1"]},
    )

    answer = adapter.answer(
        "问题",
        _concepts(),
        _context,
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert answer.sources[0].quote == "拥塞控制用于避免网络过载。"
    assert answer.examples[0].stem == "拥塞控制的作用是什么？"
    assert answer.examples[0].answer == "避免网络过载。"


def test_partial_course_coverage_combines_course_evidence_and_web_search() -> None:
    adapter, transport = _adapter(
        {
            "matches": [{"concept_id": "concept-1", "confidence": 0.91}],
            "coverage": "partial",
            "uncovered_parts": ["最新标准变化"],
            "citation_ids": [],
        },
        _web_response(),
        {
            "analysis": "课程原文给出基本作用，外部资料补充了最新标准。",
            "citation_ids": ["evidence-1"],
        },
        web_search=True,
    )

    answer = adapter.answer(
        "拥塞控制的作用和最新标准是什么？",
        _concepts(),
        _context,
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert answer.coverage == "partial"
    assert "部分" in answer.warning and "核对事实" in answer.warning
    assert answer.sources[0].evidence_id == "evidence-1"
    assert answer.external_sources[0].url == (
        "https://www.rfc-editor.org/rfc/rfc5681"
    )
    assert [call["url"] for call in transport.calls] == [
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com/responses",
        "https://api.deepseek.com/chat/completions",
    ]


def test_no_course_match_returns_warned_web_answer() -> None:
    adapter, transport = _adapter(
        {
            "matches": [],
            "coverage": "none",
            "uncovered_parts": ["量子网络"],
            "citation_ids": [],
        },
        _web_response("外部资料介绍了量子网络的基本概念。"),
        web_search=True,
    )

    answer = adapter.answer(
        "量子网络是什么？",
        _concepts(),
        _context,
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert answer.status == "answered"
    assert answer.coverage == "none"
    assert answer.matched_concepts == ()
    assert answer.sources == ()
    assert answer.analysis == "外部资料介绍了量子网络的基本概念。"
    assert "没有课程知识点" in answer.warning and "核对事实" in answer.warning
    assert answer.external_sources[0].title == "RFC 5681"
    assert len(transport.calls) == 2


def test_failed_web_search_falls_back_with_stronger_unverified_warning() -> None:
    adapter, _ = _adapter(
        {
            "matches": [],
            "coverage": "none",
            "uncovered_parts": ["量子网络"],
            "citation_ids": [],
        },
        DeepSeekHTTPResponse(503, b"private failure", {}),
        {"analysis": "模型已有知识认为量子网络使用量子态传递信息。", "citation_ids": []},
        web_search=True,
    )

    answer = adapter.answer(
        "量子网络是什么？",
        _concepts(),
        _context,
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert answer.status == "answered"
    assert answer.external_sources == ()
    assert "联网检索未成功" in answer.warning
    assert "无法保证准确" in answer.warning
