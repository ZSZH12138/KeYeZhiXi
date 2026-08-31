from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.contracts.tutoring import (
    EvidenceCitation,
    STUDENT_CITATION_QUOTE_PLACEHOLDER,
    StudentFeedbackPackage,
)


NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


def _feedback_with(citation: EvidenceCitation) -> StudentFeedbackPackage:
    return StudentFeedbackPackage(
        feedback_id="feedback_1",
        task_id="task_1",
        learner_id="learner_1",
        message="Review the cited course evidence.",
        rubric_feedback=[],
        missing_concept_ids=[],
        evidence_citations=[citation],
        next_practice_item_ids=[],
        confidence=1.0,
        generated_at=NOW,
    )


@pytest.mark.parametrize(
    ("source_id", "locator"),
    [
        ("source_1", "paragraph:1"),
        ("source_1", "section:2"),
        ("source_1", "section.2.3"),
        ("source_1", "section-2-3"),
        ("source_1", "p.7"),
    ],
)
def test_governed_citation_metadata_is_student_safe(
    source_id: str,
    locator: str,
) -> None:
    citation = EvidenceCitation(
        evidence_id="evidence_1",
        source_id=source_id,
        locator=locator,
        quote=STUDENT_CITATION_QUOTE_PLACEHOLDER,
    )

    assert citation.safe_for_student() is True
    assert citation.label() == f"{source_id}@{locator}"
    assert _feedback_with(citation).safe_for_student() is True


def test_canonical_hashed_chunk_evidence_id_is_student_safe() -> None:
    citation = EvidenceCitation(
        evidence_id="evidence_chunk_" + ("a" * 64),
        source_id="source_1",
        locator="section:1",
        quote=STUDENT_CITATION_QUOTE_PLACEHOLDER,
    )

    assert citation.safe_for_student() is True
    assert citation.label() == "source_1@section:1"
    assert _feedback_with(citation).safe_for_student() is True


@pytest.mark.parametrize(
    "locator",
    [
        "slide:40",
        "slide:40;shape:19;paragraph:1",
        "slide:40;table:19;row:2;cell:3",
        "page:7;block:2",
        "paragraph:3;lines:8-12",
        "table:2;row:3;cell:4",
    ],
)
def test_release_scoped_parser_citation_is_student_safe(locator: str) -> None:
    citation = EvidenceCitation(
        evidence_id="evidence_chunk_" + ("b" * 64),
        source_id="source-version-42b1c81a21994ecb93af7a68cb077184",
        locator=locator,
        quote=STUDENT_CITATION_QUOTE_PLACEHOLDER,
    )

    assert citation.safe_for_student() is True
    assert _feedback_with(citation).safe_for_student() is True


@pytest.mark.parametrize(
    "message",
    [
        "本次练习已完成；结果仅供即时反馈，不计入学习画像。",
        "本次订正已完成；结果只更新待订正状态，不计入学习画像。",
    ],
)
def test_controlled_transient_feedback_is_student_safe(message: str) -> None:
    package = StudentFeedbackPackage(
        feedback_id="feedback_transient_task_1",
        task_id="task_1",
        learner_id="learner_1",
        message=message,
        rubric_feedback=[],
        missing_concept_ids=[],
        evidence_citations=[],
        next_practice_item_ids=[],
        confidence=1.0,
        generated_at=NOW,
    )

    assert package.safe_for_student() is True


def test_uncontrolled_citation_free_feedback_remains_blocked() -> None:
    package = StudentFeedbackPackage(
        feedback_id="feedback_transient_task_1",
        task_id="task_1",
        learner_id="learner_1",
        message="任意无引用反馈",
        rubric_feedback=[],
        missing_concept_ids=[],
        evidence_citations=[],
        next_practice_item_ids=[],
        confidence=1.0,
        generated_at=NOW,
    )

    assert package.safe_for_student() is False


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("evidence_id", "证据一"),
        ("evidence_id", " evidence_1"),
        ("evidence_id", "john-doe-home-address"),
        ("evidence_id", "evidence_john-doe-home-address"),
        ("source_id", "chapter 1"),
        ("source_id", "john-doe-home-address"),
        ("source_id", "source_john-doe-home-address"),
        ("source_id", "source_network_notes"),
        ("source_id", "source_1\nlearner@example.com"),
        ("source_id", "source_13800138000"),
        ("source_id", "source_11010519491231002X"),
        ("source_id", "this_is_a_long_natural_language_sentence"),
        ("locator", "p.0"),
        ("locator", "section:1.2-3"),
        ("locator", "page 1"),
        ("locator", "page:1"),
        ("locator", "chapter:2.3"),
        ("locator", "timecode:01:23-02:34"),
        ("locator", "chunk:john-doe-home-address"),
        ("locator", "block:final-answer"),
        ("locator", "https://example.test/course/1"),
        ("locator", "p.1 标准答案"),
        ("locator", "chunk:13800138000"),
    ],
)
def test_unapproved_citation_metadata_fails_closed(
    field: str,
    unsafe_value: str,
) -> None:
    values = {
        "evidence_id": "evidence_1",
        "source_id": "source_1",
        "locator": "p.1",
        "quote": STUDENT_CITATION_QUOTE_PLACEHOLDER,
    }
    values[field] = unsafe_value
    citation = EvidenceCitation(**values)

    assert citation.safe_for_student() is False
    assert citation.label() == "课程证据"
    assert unsafe_value not in citation.label()
    assert _feedback_with(citation).safe_for_student() is False


@pytest.mark.parametrize(
    "quote",
    [
        "A short course excerpt.",
        "张三住在北京市海淀区",
        "最终答案：A",
        "FINAL ANSWER: A",
        "The final_answer is A.",
    ],
)
def test_every_non_placeholder_quote_fails_closed(quote: str) -> None:
    citation = EvidenceCitation(
        evidence_id="evidence_1",
        source_id="source_1",
        locator="p.1",
        quote=quote,
    )

    assert citation.safe_for_student() is False
    assert citation.label() == "课程证据"
    assert _feedback_with(citation).safe_for_student() is False
