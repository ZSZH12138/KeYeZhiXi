"""Offline-only evaluation for the default-off M9 narrative candidate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable


M9_NARRATIVE_CANDIDATE_DEFAULT_ENABLED = False
NARRATIVE_EVALUATION_SCHEMA_VERSION = "m9-narrative-evaluation-v1"


@dataclass(frozen=True, slots=True)
class NarrativeEvaluationCase:
    """One text-free comparison between frozen facts and candidate output."""

    case_id: str
    expected_fact_refs: tuple[str, ...]
    actual_fact_refs: tuple[str, ...]
    expected_question_codes: tuple[str, ...]
    actual_question_codes: tuple[str, ...]
    allowed_citation_ids: tuple[str, ...]
    actual_citation_ids: tuple[str, ...]
    schema_valid: bool
    meaning_reversal: bool
    unsafe_output: bool
    teacher_accepted: bool | None = None

    def __post_init__(self) -> None:
        if type(self.case_id) is not str or not self.case_id.strip():
            raise ValueError("narrative evaluation case_id must not be blank")
        for field_name in (
            "expected_fact_refs",
            "actual_fact_refs",
            "expected_question_codes",
            "actual_question_codes",
            "allowed_citation_ids",
            "actual_citation_ids",
        ):
            values = getattr(self, field_name)
            if (
                type(values) is not tuple
                or len(values) != len(set(values))
                or any(type(value) is not str or not value for value in values)
            ):
                raise ValueError(f"{field_name} must contain unique text IDs")
        for field_name in ("schema_valid", "meaning_reversal", "unsafe_output"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be boolean")
        if self.teacher_accepted is not None and type(self.teacher_accepted) is not bool:
            raise ValueError("teacher_accepted must be boolean or null")


def evaluate_narrative_cases(
    cases: Iterable[NarrativeEvaluationCase],
) -> dict[str, object]:
    """Return aggregate, text-free evidence; no labels means insufficient."""

    values = tuple(cases)
    if not values or any(not isinstance(item, NarrativeEvaluationCase) for item in values):
        raise ValueError("narrative evaluation requires typed cases")
    if len({item.case_id for item in values}) != len(values):
        raise ValueError("narrative evaluation case IDs must be unique")

    correct_citations = sum(
        len(set(item.actual_citation_ids) & set(item.allowed_citation_ids))
        for item in values
    )
    citation_count = sum(len(item.actual_citation_ids) for item in values)
    expected_questions = sum(len(item.expected_question_codes) for item in values)
    selected_questions = sum(len(item.actual_question_codes) for item in values)
    correct_questions = sum(
        len(set(item.actual_question_codes) & set(item.expected_question_codes))
        for item in values
    )
    teacher_labels = [
        item.teacher_accepted
        for item in values
        if item.teacher_accepted is not None
    ]
    metrics = {
        "schema_valid_rate": sum(item.schema_valid for item in values) / len(values),
        "fact_fidelity_rate": sum(
            item.schema_valid
            and not item.meaning_reversal
            and item.actual_fact_refs == item.expected_fact_refs
            for item in values
        )
        / len(values),
        "citation_precision": (
            correct_citations / citation_count if citation_count else 1.0
        ),
        "question_selection_precision": (
            correct_questions / selected_questions if selected_questions else 1.0
        ),
        "question_selection_recall": (
            correct_questions / expected_questions if expected_questions else 1.0
        ),
        "unsafe_output_rate": sum(item.unsafe_output for item in values) / len(values),
        "meaning_reversal_rate": sum(item.meaning_reversal for item in values) / len(values),
        "teacher_acceptance_rate": (
            sum(bool(value) for value in teacher_labels) / len(teacher_labels)
            if teacher_labels
            else None
        ),
    }
    identity = [
        {
            "case_id": item.case_id,
            "schema_valid": item.schema_valid,
            "meaning_reversal": item.meaning_reversal,
            "unsafe_output": item.unsafe_output,
            "teacher_label_present": item.teacher_accepted is not None,
        }
        for item in sorted(values, key=lambda item: item.case_id)
    ]
    checksum = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": NARRATIVE_EVALUATION_SCHEMA_VERSION,
        "status": "ready" if teacher_labels else "insufficient_data",
        "candidate_default_enabled": M9_NARRATIVE_CANDIDATE_DEFAULT_ENABLED,
        "observation_count": len(values),
        "teacher_label_count": len(teacher_labels),
        "metrics": metrics,
        "evaluation_checksum": checksum,
    }


__all__ = [
    "M9_NARRATIVE_CANDIDATE_DEFAULT_ENABLED",
    "NARRATIVE_EVALUATION_SCHEMA_VERSION",
    "NarrativeEvaluationCase",
    "evaluate_narrative_cases",
]
