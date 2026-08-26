"""Deterministic, per-question-isolated parser for the fixed TXT format."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


QuestionType = Literal["choice", "fill_blank", "subjective"]
_QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_OPTION_RE = re.compile(r"^option\.([A-Z])$")


@dataclass(frozen=True, slots=True)
class ParsedQuestion:
    source_id: str
    question_id: str
    question_type: QuestionType
    stem: str
    options: dict[str, str]
    accepted_answers: tuple[str, ...]
    rubric: str | None
    explanation: str
    ordinal: int
    locator: str


@dataclass(frozen=True, slots=True)
class QuestionParseIssue:
    code: str
    message: str
    locator: str


@dataclass(frozen=True, slots=True)
class QuestionFileParseResult:
    source_id: str
    questions: tuple[ParsedQuestion, ...]
    issues: tuple[QuestionParseIssue, ...]


def parse_question_file(source_id: str, text: str) -> QuestionFileParseResult:
    """Parse valid siblings even when another question block is malformed."""

    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id must be non-empty")
    if not isinstance(text, str):
        raise TypeError("question text must be a string")
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    blocks, structural_issues = _collect_blocks(normalized.split("\n"))
    questions: list[ParsedQuestion] = []
    issues = list(structural_issues)
    seen_ids: set[str] = set()

    for start, end, lines in blocks:
        locator = f"lines:{start}-{end}"
        try:
            question = _parse_block(
                source_id=source_id,
                lines=lines,
                ordinal=len(questions) + 1,
                locator=locator,
            )
        except _QuestionBlockError as error:
            issues.append(QuestionParseIssue(error.code, str(error), locator))
            continue
        if question.question_id in seen_ids:
            issues.append(
                QuestionParseIssue(
                    "QUESTION_ID_DUPLICATED",
                    "question id is duplicated in the file",
                    locator,
                )
            )
            continue
        seen_ids.add(question.question_id)
        questions.append(question)

    # Re-number only valid questions, preserving stable source order.
    questions = [
        ParsedQuestion(
            source_id=question.source_id,
            question_id=question.question_id,
            question_type=question.question_type,
            stem=question.stem,
            options=dict(question.options),
            accepted_answers=question.accepted_answers,
            rubric=question.rubric,
            explanation=question.explanation,
            ordinal=index,
            locator=question.locator,
        )
        for index, question in enumerate(questions, start=1)
    ]
    return QuestionFileParseResult(source_id, tuple(questions), tuple(issues))


class _QuestionBlockError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _collect_blocks(lines: list[str]) -> tuple[list[tuple[int, int, list[str]]], list[QuestionParseIssue]]:
    blocks: list[tuple[int, int, list[str]]] = []
    issues: list[QuestionParseIssue] = []
    start: int | None = None
    content: list[str] = []
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if line == "[QUESTION]":
            if start is not None:
                issues.append(
                    QuestionParseIssue(
                        "QUESTION_BLOCK_NESTED",
                        "question block starts before the previous block closes",
                        f"lines:{start}-{line_number - 1}",
                    )
                )
            start = line_number
            content = []
        elif line == "[/QUESTION]":
            if start is None:
                issues.append(
                    QuestionParseIssue(
                        "QUESTION_BLOCK_CLOSE_WITHOUT_OPEN",
                        "question block closes without an opening marker",
                        f"lines:{line_number}-{line_number}",
                    )
                )
            else:
                blocks.append((start, line_number, list(content)))
                start = None
                content = []
        elif start is not None:
            content.append(raw_line)
        elif line:
            issues.append(
                QuestionParseIssue(
                    "QUESTION_TEXT_OUTSIDE_BLOCK",
                    "non-empty text appears outside a question block",
                    f"lines:{line_number}-{line_number}",
                )
            )
    if start is not None:
        issues.append(
            QuestionParseIssue(
                "QUESTION_BLOCK_UNCLOSED",
                "question block is not closed",
                f"lines:{start}-{len(lines)}",
            )
        )
    return blocks, issues


def _parse_block(*, source_id: str, lines: list[str], ordinal: int, locator: str) -> ParsedQuestion:
    fields: dict[str, str] = {}
    options: dict[str, str] = {}
    for raw_line in lines:
        if not raw_line.strip():
            continue
        if ":" not in raw_line:
            raise _QuestionBlockError("QUESTION_FIELD_INVALID", "question field must contain ':'")
        key, value = (part.strip() for part in raw_line.split(":", 1))
        option_match = _OPTION_RE.fullmatch(key)
        if option_match:
            option_key = option_match.group(1)
            if option_key in options or not value:
                raise _QuestionBlockError("QUESTION_OPTION_INVALID", "question option is empty or duplicated")
            options[option_key] = value
            continue
        if key not in {"id", "type", "stem", "answer", "rubric", "explanation"}:
            raise _QuestionBlockError("QUESTION_FIELD_UNKNOWN", "question field is not supported")
        if key in fields or not value:
            raise _QuestionBlockError("QUESTION_FIELD_INVALID", "question field is empty or duplicated")
        fields[key] = value

    required = {"id", "type", "stem", "answer", "explanation"}
    if not required.issubset(fields):
        raise _QuestionBlockError("QUESTION_REQUIRED_FIELD_MISSING", "question is missing a required field")
    if not _QUESTION_ID_RE.fullmatch(fields["id"]):
        raise _QuestionBlockError("QUESTION_ID_INVALID", "question id is invalid")
    question_type = fields["type"]
    if question_type not in {"choice", "fill_blank", "subjective"}:
        raise _QuestionBlockError("QUESTION_TYPE_INVALID", "question type is not supported")
    answers = tuple(value.strip() for value in fields["answer"].split("|") if value.strip())
    if not answers:
        raise _QuestionBlockError("QUESTION_ANSWER_INVALID", "question answer is empty")
    if question_type == "choice":
        if len(options) < 2 or any(answer not in options for answer in answers):
            raise _QuestionBlockError("QUESTION_CHOICE_INVALID", "choice answer must refer to declared options")
    elif options:
        raise _QuestionBlockError("QUESTION_OPTION_INVALID", "only choice questions may declare options")
    if question_type == "subjective" and not fields.get("rubric"):
        raise _QuestionBlockError("QUESTION_RUBRIC_REQUIRED", "subjective question requires a rubric")

    return ParsedQuestion(
        source_id=source_id,
        question_id=fields["id"],
        question_type=question_type,
        stem=fields["stem"],
        options=options,
        accepted_answers=answers,
        rubric=fields.get("rubric"),
        explanation=fields["explanation"],
        ordinal=ordinal,
        locator=locator,
    )
