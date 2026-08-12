"""Business equivalence checks for replaying immutable M8 writes."""

from __future__ import annotations

from typing import Any

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)


def same_paper_generation(
    first: AssessmentPaper,
    second: AssessmentPaper,
) -> bool:
    """Compare frozen paper content while excluding first-write metadata."""

    return _paper_request_payload(first) == _paper_request_payload(second)


def same_frozen_generation(
    first: FrozenAssessmentRecord,
    second: FrozenAssessmentRecord,
) -> bool:
    """Compare the complete business request that produced a frozen paper."""

    return (
        first.schema_version == second.schema_version
        and first.course_id == second.course_id
        and first.class_id == second.class_id
        and first.frozen_rubrics == second.frozen_rubrics
        and same_paper_generation(first.paper, second.paper)
    )


def same_score_audit_result(
    first: ScoreAuditRecord,
    second: ScoreAuditRecord,
) -> bool:
    """Compare one score decision while excluding its first-write timestamp."""

    return _score_audit_payload(first) == _score_audit_payload(second)


def same_scoring_result(
    first: ScoringResultBundle,
    second: ScoringResultBundle,
) -> bool:
    """Compare one complete scoring decision without regenerated timestamps."""

    return _scoring_result_payload(first) == _scoring_result_payload(second)


def _paper_request_payload(paper: AssessmentPaper) -> dict[str, Any]:
    payload = paper.model_dump(mode="python")
    return {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "immutable_checksum"}
    }


def _score_audit_payload(record: ScoreAuditRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="python")
    return {key: value for key, value in payload.items() if key != "created_at"}


def _scoring_result_payload(bundle: ScoringResultBundle) -> dict[str, Any]:
    payload = bundle.model_dump(mode="python")
    return {
        **{
            key: value
            for key, value in payload.items()
            if key
            not in {
                "finalized_at",
                "score_audit_records",
                "learning_events",
                "remediation_plan",
            }
        },
        "score_audit_records": [
            {
                key: value
                for key, value in record.items()
                if key != "created_at"
            }
            for record in payload["score_audit_records"]
        ],
        "learning_events": [
            {
                key: value
                for key, value in event.items()
                if key != "occurred_at"
            }
            for event in payload["learning_events"]
        ],
        "remediation_plan": {
            key: value
            for key, value in payload["remediation_plan"].items()
            if key != "created_at"
        },
    }


__all__ = [
    "same_frozen_generation",
    "same_paper_generation",
    "same_score_audit_result",
    "same_scoring_result",
]
