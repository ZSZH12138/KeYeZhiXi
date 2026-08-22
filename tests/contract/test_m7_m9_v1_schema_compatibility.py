from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from course_insight.contracts.analytics import (
    IndividualReport,
    TeacherAnalyticsBundle,
)
from course_insight.contracts.tutoring import (
    EvidenceCitation,
    StudentFeedbackPackage,
)


SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts" / "schemas"


def _load_schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text("utf-8"))


@pytest.mark.parametrize(
    ("contract_type", "name"),
    [
        (EvidenceCitation, "EvidenceCitation"),
        (StudentFeedbackPackage, "StudentFeedbackPackage"),
        (IndividualReport, "IndividualReport"),
        (TeacherAnalyticsBundle, "TeacherAnalyticsBundle"),
    ],
)
def test_checked_in_v1_schema_matches_official_contract_export(
    contract_type,
    name: str,
) -> None:
    assert _load_schema(name) == contract_type.model_json_schema(
        mode="validation"
    )


@pytest.mark.parametrize(
    "schema",
    [
        EvidenceCitation.model_json_schema(mode="validation"),
        StudentFeedbackPackage.model_json_schema(mode="validation")["$defs"][
            "EvidenceCitation"
        ],
    ],
)
def test_v1_citation_quote_is_a_required_nonempty_string(schema) -> None:
    assert "quote" in schema["required"]
    assert schema["properties"]["quote"] == {
        "minLength": 1,
        "title": "Quote",
        "type": "string",
    }


def test_v1_citation_without_quote_is_rejected_by_the_public_contract() -> None:
    with pytest.raises(ValidationError, match="quote"):
        EvidenceCitation(
            evidence_id="evidence_1",
            source_id="source_1",
            locator="p.1",
        )


@pytest.mark.parametrize(
    "schema",
    [
        IndividualReport.model_json_schema(mode="validation"),
        TeacherAnalyticsBundle.model_json_schema(mode="validation")["$defs"][
            "IndividualReport"
        ],
    ],
)
def test_v1_recent_score_is_a_required_number(schema) -> None:
    assert "recent_score" in schema["required"]
    assert schema["properties"]["recent_score"] == {
        "minimum": 0.0,
        "title": "Recent Score",
        "type": "number",
    }


def test_v1_individual_report_rejects_a_null_recent_score() -> None:
    with pytest.raises(ValidationError, match="recent_score"):
        IndividualReport(
            learner_id="learner_1",
            overall_mastery=1.0,
            weak_concept_ids=[],
            active_misconception_ids=[],
            recent_score=None,
            review_required_count=0,
        )
