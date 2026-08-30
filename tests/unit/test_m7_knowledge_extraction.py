from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.contracts.knowledge_ingestion import KnowledgeExtractionBatch
from course_insight.infrastructure.deepseek import (
    DeepSeekClient,
    DeepSeekHTTPResponse,
)
from course_insight.modules.m7_local_model.knowledge_extraction import (
    DeepSeekKnowledgeExtractionAdapter,
)


NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


class _FakeTransport:
    def __init__(self, content: str, *additional_contents: str) -> None:
        self.contents = [content, *additional_contents]
        self.calls: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        del url, headers, max_response_bytes
        self.calls.append(json.loads(payload))
        self.timeouts.append(timeout_seconds)
        content = (
            self.contents.pop(0)
            if len(self.contents) > 1
            else self.contents[0]
        )
        return DeepSeekHTTPResponse(
            status_code=200,
            body=json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": content},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={},
        )


class _TimeoutTransport:
    def __init__(self) -> None:
        self.call_count = 0

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        del url, headers, payload, timeout_seconds, max_response_bytes
        self.call_count += 1
        raise TimeoutError


def _batch() -> KnowledgeExtractionBatch:
    return KnowledgeExtractionBatch(
        batch_id="batch-1",
        course_id="course-1",
        chunks=[
            ContentChunk(
                chunk_id="chunk-1",
                source_id="source-version-1",
                text="拥塞控制采用慢启动算法。",
                locator="page:1;block:1",
                concept_hints=[],
                sha256="a" * 64,
            )
        ],
        max_chars=1_000,
    )


def _model_ref() -> LLMModelRef:
    return LLMModelRef(
        model_name="deepseek-v4-flash",
        model_version="runtime-api",
        status="configured",
    )


def _concept(name: str, quote: str = "拥塞控制") -> dict[str, object]:
    return {
        "name": name,
        "description": f"{name}的课程内定义",
        "aliases": [],
        "evidence": [
            {
                "chunk_id": "chunk-1",
                "quote": quote,
                "relation_type": "definition",
            }
        ],
    }


def _adapter(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object] | str):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    transport = _FakeTransport(content)
    client = DeepSeekClient(
        transport=transport,
        max_attempts=1,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    return DeepSeekKnowledgeExtractionAdapter(client), transport


def test_prompt_requires_all_concepts_and_accepts_multiple_results(monkeypatch) -> None:
    adapter, transport = _adapter(
        monkeypatch,
        {
            "concepts": [_concept("拥塞控制"), _concept("慢启动", "慢启动")],
            "citation_ids": ["chunk-1"],
        },
    )

    result = adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    assert [candidate.name for candidate in result.candidates] == ["拥塞控制", "慢启动"]
    system_prompt = transport.calls[0]["messages"][0]["content"]
    assert "所有" in system_prompt
    assert "零个、一个或多个" in system_prompt
    assert "quote" in system_prompt
    assert "span_start" not in system_prompt
    assert "输出前逐项自检" in system_prompt
    assert "quote 必须能在对应 chunks.text 中逐字查找到" in system_prompt


def test_invalid_output_retries_five_times_then_succeeds_with_full_timeout(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    invalid = json.dumps(
        {
            "concepts": [_concept("拥塞控制", "模型改写的非原文引用")],
            "citation_ids": ["chunk-1"],
        },
        ensure_ascii=False,
    )
    valid = json.dumps(
        {
            "concepts": [_concept("拥塞控制")],
            "citation_ids": ["chunk-1"],
        },
        ensure_ascii=False,
    )
    transport = _FakeTransport(invalid, invalid, invalid, invalid, invalid, valid)
    client = DeepSeekClient(
        transport=transport,
        timeout_seconds=30.0,
        max_attempts=1,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    retries: list[tuple[int, int, str]] = []
    adapter = DeepSeekKnowledgeExtractionAdapter(client)

    result = adapter.extract(
        _batch(),
        model_ref=_model_ref(),
        created_at=NOW,
        on_retry=lambda attempt, limit, reason: retries.append(
            (attempt, limit, reason)
        ),
    )

    assert [candidate.name for candidate in result.candidates] == ["拥塞控制"]
    assert len(transport.calls) == 6
    assert transport.timeouts == [30.0, 30.0, 30.0, 30.0, 30.0, 30.0]
    assert retries == [
        (1, 5, "evidence_quote_not_found"),
        (2, 5, "evidence_quote_not_found"),
        (3, 5, "evidence_quote_not_found"),
        (4, 5, "evidence_quote_not_found"),
        (5, 5, "evidence_quote_not_found"),
    ]
    retry_payload = json.loads(transport.calls[-1]["messages"][1]["content"])
    assert retry_payload["retry_context"] == {
        "attempt": 5,
        "limit": 5,
        "validation_code": "evidence_quote_not_found",
    }


def test_invalid_output_fails_only_after_five_retries(monkeypatch) -> None:
    adapter, transport = _adapter(
        monkeypatch,
        {
            "concepts": [_concept("不存在", "不在原文中的引文")],
            "citation_ids": ["chunk-1"],
        },
    )

    with pytest.raises(DomainError) as raised:
        adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    assert raised.value.code == "KNOWLEDGE_EXTRACTION_OUTPUT_INVALID"
    assert raised.value.details["retry_count"] == 5
    assert raised.value.details["attempt_count"] == 6
    assert len(transport.calls) == 6


def test_exact_quote_is_converted_to_a_locally_computed_span(monkeypatch) -> None:
    adapter, _ = _adapter(
        monkeypatch,
        {
            "concepts": [_concept("慢启动", "采用慢启动算法")],
            "citation_ids": ["chunk-1"],
        },
    )

    result = adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    reference = result.candidates[0].evidence[0]
    assert reference.span_start == 4
    assert reference.span_end == 11


def test_grounded_evidence_ignores_disagreeing_redundant_citation_summary(
    monkeypatch,
) -> None:
    adapter, transport = _adapter(
        monkeypatch,
        {
            "concepts": [_concept("拥塞控制")],
            "citation_ids": [],
        },
    )

    result = adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    assert [candidate.name for candidate in result.candidates] == ["拥塞控制"]
    assert result.candidates[0].evidence[0].chunk_id == "chunk-1"
    assert len(transport.calls) == 1


def test_quote_with_layout_whitespace_maps_to_the_original_source_span(
    monkeypatch,
) -> None:
    source_text = "拥塞控制采用\n慢启动算法。"
    batch = KnowledgeExtractionBatch(
        batch_id="batch-layout-whitespace",
        course_id="course-1",
        chunks=[
            ContentChunk(
                chunk_id="chunk-1",
                source_id="source-version-1",
                text=source_text,
                locator="slide:1",
                concept_hints=[],
                sha256="b" * 64,
            )
        ],
        max_chars=1_000,
    )
    adapter, transport = _adapter(
        monkeypatch,
        {
            "concepts": [_concept("慢启动", "拥塞控制采用 慢启动算法。")],
            "citation_ids": ["chunk-1"],
        },
    )

    result = adapter.extract(batch, model_ref=_model_ref(), created_at=NOW)

    reference = result.candidates[0].evidence[0]
    assert (reference.span_start, reference.span_end) == (0, len(source_text))
    assert len(transport.calls) == 1


def test_empty_concept_array_is_valid(monkeypatch) -> None:
    adapter, _ = _adapter(monkeypatch, {"concepts": [], "citation_ids": []})

    result = adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    assert result.candidates == []
    assert result.possibly_truncated is False


def test_thinking_timeout_falls_back_to_non_thinking_extraction(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    primary_transport = _TimeoutTransport()
    primary = DeepSeekClient(
        transport=primary_transport,
        max_attempts=1,
        thinking_enabled=True,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    fallback_transport = _FakeTransport(
        json.dumps(
            {
                "concepts": [_concept("拥塞控制")],
                "citation_ids": ["chunk-1"],
            },
            ensure_ascii=False,
        )
    )
    fallback = DeepSeekClient(
        transport=fallback_transport,
        max_attempts=1,
        thinking_enabled=False,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    adapter = DeepSeekKnowledgeExtractionAdapter(
        primary,
        fallback_client=fallback,
    )

    result = adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    assert [candidate.name for candidate in result.candidates] == ["拥塞控制"]
    assert primary_transport.call_count == 1
    assert fallback_transport.calls[0]["thinking"] == {"type": "disabled"}


def test_quote_absent_from_chunk_is_rejected(
    monkeypatch,
) -> None:
    adapter, _ = _adapter(
        monkeypatch,
        {"concepts": [_concept("不存在", "不在原文中的引文")], "citation_ids": ["chunk-1"]},
    )

    with pytest.raises(DomainError, match="knowledge extraction"):
        adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)


def test_exactly_one_hundred_concepts_sets_overflow_signal(monkeypatch) -> None:
    concepts = [_concept(f"知识点{index}") for index in range(100)]
    adapter, _ = _adapter(
        monkeypatch,
        {"concepts": concepts, "citation_ids": ["chunk-1"]},
    )

    result = adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)

    assert len(result.candidates) == 100
    assert result.possibly_truncated is True


@pytest.mark.parametrize(
    "payload",
    [
        {"concepts": [_concept("未知来源") | {"evidence": [{"chunk_id": "unknown", "quote": "拥塞控制", "relation_type": "definition"}]}], "citation_ids": ["unknown"]},
        {"concepts": [_concept("空引用", "")], "citation_ids": ["chunk-1"]},
        {"concepts": [_concept("缺少引用")]},
        '{"concepts":[],"concepts":[],"citation_ids":[]}',
    ],
)
def test_invalid_or_ungrounded_output_is_rejected(monkeypatch, payload) -> None:
    adapter, _ = _adapter(monkeypatch, payload)

    with pytest.raises(DomainError, match="knowledge extraction"):
        adapter.extract(_batch(), model_ref=_model_ref(), created_at=NOW)
