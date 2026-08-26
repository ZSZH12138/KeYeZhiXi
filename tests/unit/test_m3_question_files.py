from __future__ import annotations

from course_insight.modules.m3_knowledge_bundle.question_files import (
    parse_question_file,
)


VALID_TEXT = """[QUESTION]
id: net-choice-001
type: choice
stem: TCP 慢启动阶段，拥塞窗口通常如何增长？
option.A: 每个 RTT 约增加 1 MSS
option.B: 每个 RTT 近似翻倍
answer: B
explanation: 按 RTT 观察呈近似指数增长。
[/QUESTION]
[QUESTION]
id: net-fill-001
type: fill_blank
stem: TCP 的拥塞窗口称为 ____。
answer: 拥塞窗口|cwnd
explanation: 两种写法均接受。
[/QUESTION]
[QUESTION]
id: net-subjective-001
type: subjective
stem: 比较流量控制与拥塞控制。
answer: 流量控制保护接收端；拥塞控制保护网络路径。
rubric: 分别说明控制对象和作用范围，共 10 分。
explanation: 可结合两个窗口说明。
[/QUESTION]"""


def test_parses_choice_fill_blank_and_subjective_questions() -> None:
    result = parse_question_file("source-questions", VALID_TEXT)

    assert [question.question_type for question in result.questions] == [
        "choice",
        "fill_blank",
        "subjective",
    ]
    assert result.questions[0].options == {"A": "每个 RTT 约增加 1 MSS", "B": "每个 RTT 近似翻倍"}
    assert result.questions[1].accepted_answers == ("拥塞窗口", "cwnd")
    assert result.questions[2].rubric.startswith("分别说明")
    assert result.issues == ()
    assert result.questions[0].locator == "lines:1-9"


def test_one_malformed_question_does_not_discard_valid_sibling() -> None:
    text = """[QUESTION]
id: broken
type: choice
stem: 缺少答案
option.A: A
option.B: B
[/QUESTION]
[QUESTION]
id: net-fill-001
type: fill_blank
stem: TCP 的拥塞窗口称为 ____。
answer: 拥塞窗口|cwnd
explanation: 两种写法均接受。
[/QUESTION]
"""

    result = parse_question_file("source-questions", text)

    assert any(question.question_id == "net-fill-001" for question in result.questions)
    assert any(issue.code == "QUESTION_REQUIRED_FIELD_MISSING" for issue in result.issues)


def test_duplicate_question_id_is_isolated() -> None:
    first_block = VALID_TEXT[: VALID_TEXT.index("[QUESTION]", 1)]
    duplicated = first_block + first_block

    result = parse_question_file("source-questions", duplicated)

    assert len(result.questions) == 1
    assert result.issues[0].code == "QUESTION_ID_DUPLICATED"


def test_unclosed_question_reports_line_locator() -> None:
    result = parse_question_file(
        "source-questions",
        "[QUESTION]\nid: broken\ntype: fill_blank\nstem: 未闭合",
    )

    assert result.questions == ()
    assert result.issues[0].code == "QUESTION_BLOCK_UNCLOSED"
    assert result.issues[0].locator == "lines:1-4"
