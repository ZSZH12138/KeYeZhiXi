from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Mapping

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.contracts.knowledge_ingestion import (
    KnowledgeEvidenceRef,
    MergedKnowledgeConcept,
)
from course_insight.infrastructure.deepseek import DeepSeekClient, DeepSeekHTTPResponse
from course_insight.modules.m3_knowledge_bundle.question_files import ParsedQuestion
from course_insight.modules.m7_local_model.question_linking import (
    DeepSeekQuestionLinkingAdapter,
)


NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


class _Transport:
    def __init__(self, structured: dict[str, object]) -> None:
        self.structured = structured

    def post_json(self, *, url: str, headers: Mapping[str, str], payload: bytes, timeout_seconds: float, max_response_bytes: int) -> DeepSeekHTTPResponse:
        del url, headers, payload, timeout_seconds, max_response_bytes
        content = json.dumps(self.structured, ensure_ascii=False)
        return DeepSeekHTTPResponse(
            200,
            json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode(),
            {},
        )


def _evidence(name: str) -> KnowledgeEvidenceRef:
    return KnowledgeEvidenceRef(
        source_id=f"source-{name}",
        source_version_id=f"version-{name}",
        chunk_id=f"chunk-{name}",
        locator=f"page:{name}",
        span_start=0,
        span_end=4,
        relation_type="definition",
    )


def _concept(identifier: str, name: str, evidence: list[KnowledgeEvidenceRef]) -> MergedKnowledgeConcept:
    return MergedKnowledgeConcept(
        concept_id=identifier,
        course_id="course-1",
        name=name,
        description=f"{name}定义",
        aliases=[],
        evidence=evidence,
    )


def _question() -> ParsedQuestion:
    return ParsedQuestion(
        source_id="question-source",
        question_id="question-1",
        question_type="subjective",
        stem="比较慢启动与拥塞控制。",
        options={},
        accepted_answers=("...",),
        rubric="说明二者关系。",
        explanation="课程内作答。",
        ordinal=1,
        locator="lines:1-8",
    )


def _adapter(monkeypatch, structured):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    client = DeepSeekClient(
        transport=_Transport(structured),
        max_attempts=1,
        sleep=lambda _: None,
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
    )
    return DeepSeekQuestionLinkingAdapter(client)


def _model_ref() -> LLMModelRef:
    return LLMModelRef(model_name="deepseek-v4-flash", model_version="runtime-api", status="configured")


def test_linker_accepts_multiple_labels_and_carries_full_concept_sources(monkeypatch) -> None:
    first_sources = [_evidence("a"), _evidence("b")]
    concepts = [
        _concept("concept-1", "慢启动", first_sources),
        _concept("concept-2", "拥塞控制", [_evidence("c")]),
    ]
    adapter = _adapter(
        monkeypatch,
        {
            "links": [
                {"concept_id": "concept-1", "confidence": 0.91},
                {"concept_id": "concept-2", "confidence": 0.82},
            ],
            "citation_ids": ["concept-1", "concept-2"],
        },
    )

    links = adapter.link(_question(), concepts, model_ref=_model_ref(), created_at=NOW)

    assert [link.concept_id for link in links] == ["concept-1", "concept-2"]
    assert [ref.source_version_id for ref in links[0].evidence] == ["version-a", "version-b"]
    assert all(link.status == "usable" for link in links)


def test_unknown_or_retired_concept_id_is_rejected(monkeypatch) -> None:
    adapter = _adapter(
        monkeypatch,
        {"links": [{"concept_id": "retired-concept", "confidence": 0.9}], "citation_ids": ["retired-concept"]},
    )

    with pytest.raises(DomainError, match="question concept linking"):
        adapter.link(
            _question(),
            [_concept("concept-1", "慢启动", [_evidence("a")])],
            model_ref=_model_ref(),
            created_at=NOW,
        )


def test_low_confidence_link_is_marked_for_review(monkeypatch) -> None:
    adapter = _adapter(
        monkeypatch,
        {"links": [{"concept_id": "concept-1", "confidence": 0.4}], "citation_ids": ["concept-1"]},
    )

    links = adapter.link(
        _question(),
        [_concept("concept-1", "慢启动", [_evidence("a")])],
        model_ref=_model_ref(),
        created_at=NOW,
    )

    assert links[0].status == "needs_review"
