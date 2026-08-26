from __future__ import annotations

from course_insight.contracts.knowledge_ingestion import (
    KnowledgeCandidate,
    KnowledgeEvidenceRef,
)
from course_insight.modules.m3_knowledge_bundle.concept_merge import (
    merge_knowledge_candidates,
)


def _evidence(source: str, *, start: int = 0, end: int = 4) -> KnowledgeEvidenceRef:
    return KnowledgeEvidenceRef(
        source_id=f"file-{source}",
        source_version_id=f"version-{source}",
        chunk_id=f"chunk-{source}",
        locator=f"page:{source};block:1",
        span_start=start,
        span_end=end,
        relation_type="definition",
    )


def _candidate(identifier: str, description: str, evidence: list[KnowledgeEvidenceRef]) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=identifier,
        name="拥塞控制",
        description=description,
        aliases=["网络拥塞控制"],
        evidence=evidence,
    )


def test_merge_keeps_source_union_from_two_files_and_deduplicates() -> None:
    source_a = _evidence("a")
    source_b = _evidence("b")
    candidates = [
        _candidate("candidate-a", "用于避免网络过载。", [source_a]),
        _candidate("candidate-b", "通过调节发送速率来避免网络过载。", [source_b, source_a]),
    ]

    concepts = merge_knowledge_candidates("course-1", candidates)

    assert len(concepts) == 1
    assert concepts[0].description == "通过调节发送速率来避免网络过载。"
    assert [reference.source_version_id for reference in concepts[0].evidence] == [
        "version-a",
        "version-b",
    ]


def test_merge_is_deterministic_when_candidate_order_changes() -> None:
    candidates = [
        _candidate("candidate-a", "定义一", [_evidence("a")]),
        _candidate("candidate-b", "更完整的定义二", [_evidence("b")]),
    ]

    forward = merge_knowledge_candidates("course-1", candidates)
    reverse = merge_knowledge_candidates("course-1", list(reversed(candidates)))

    assert [item.model_dump(mode="json") for item in forward] == [
        item.model_dump(mode="json") for item in reverse
    ]


def test_distinct_names_remain_distinct_concepts() -> None:
    first = _candidate("candidate-a", "定义一", [_evidence("a")])
    second = KnowledgeCandidate(
        candidate_id="candidate-c",
        name="慢启动",
        description="拥塞窗口逐步增加。",
        aliases=[],
        evidence=[_evidence("c")],
    )

    concepts = merge_knowledge_candidates("course-1", [second, first])

    assert [concept.name for concept in concepts] == ["慢启动", "拥塞控制"]
