"""Checksum-pinned loader for text-free M9 narrative evaluation cases."""

from __future__ import annotations

from pathlib import Path

from course_insight.modules.m9_teacher_analytics.narrative_evaluation import (
    NarrativeEvaluationCase,
    evaluate_narrative_cases,
)

from ._common import (
    EvaluationInputError,
    load_jsonl_bytes,
    require_exact_keys,
    require_local_regular_file,
    require_safe_id,
    require_sha256,
    sha256_bytes,
)


_FIELDS = frozenset(
    {
        "case_id",
        "expected_fact_refs",
        "actual_fact_refs",
        "expected_question_codes",
        "actual_question_codes",
        "allowed_citation_ids",
        "actual_citation_ids",
        "schema_valid",
        "meaning_reversal",
        "unsafe_output",
        "teacher_accepted",
    }
)


def load_narrative_cases(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[NarrativeEvaluationCase, ...]:
    expected = require_sha256(expected_sha256, field_name="narrative data SHA-256")
    checked = require_local_regular_file(path, max_bytes=64 * 1024 * 1024)
    payload = checked.read_bytes()
    if sha256_bytes(payload) != expected:
        raise EvaluationInputError("narrative data SHA-256 mismatch")
    rows = load_jsonl_bytes(payload, field_name="narrative cases", max_records=100_000)
    result: list[NarrativeEvaluationCase] = []
    for row in rows:
        require_exact_keys(row, _FIELDS, field_name="narrative case")
        result.append(
            NarrativeEvaluationCase(
                case_id=require_safe_id(row["case_id"], field_name="case_id"),
                expected_fact_refs=_id_tuple(row["expected_fact_refs"]),
                actual_fact_refs=_id_tuple(row["actual_fact_refs"]),
                expected_question_codes=_id_tuple(row["expected_question_codes"]),
                actual_question_codes=_id_tuple(row["actual_question_codes"]),
                allowed_citation_ids=_id_tuple(row["allowed_citation_ids"]),
                actual_citation_ids=_id_tuple(row["actual_citation_ids"]),
                schema_valid=_boolean(row["schema_valid"]),
                meaning_reversal=_boolean(row["meaning_reversal"]),
                unsafe_output=_boolean(row["unsafe_output"]),
                teacher_accepted=_optional_boolean(row["teacher_accepted"]),
            )
        )
    return tuple(result)


def _id_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        raise EvaluationInputError("narrative ID collections must be lists")
    return tuple(require_safe_id(item, field_name="narrative ID") for item in value)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise EvaluationInputError("narrative flags must be boolean")
    return value


def _optional_boolean(value: object) -> bool | None:
    if value is None:
        return None
    return _boolean(value)


__all__ = ["evaluate_narrative_cases", "load_narrative_cases"]
