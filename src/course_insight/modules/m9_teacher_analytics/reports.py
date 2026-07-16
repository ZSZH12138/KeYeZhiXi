"""Pure report builders that preserve existing M5 and M8 values."""

from __future__ import annotations

import math

from course_insight.contracts.analytics import ClassReport, IndividualReport
from course_insight.contracts.assessment import (
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.state import StateUpdateResult


def latest_audits(bundle: ScoringResultBundle) -> list[ScoreAuditRecord]:
    """Return independent latest audit versions in stable first-seen order."""

    order = list(dict.fromkeys(record.audit_id for record in bundle.score_audit_records))
    return [bundle.get_audit_record(audit_id) for audit_id in order]


def build_class_report(
    scoring: ScoringResultBundle,
    state: StateUpdateResult,
) -> ClassReport:
    """Copy class state and summarize existing latest audit scores."""

    audits = latest_audits(scoring)
    scores = [record.total_score for record in audits]
    total = math.fsum(scores) if scores else 0.0
    statistics = {
        "audit_count": float(len(scores)),
        "score_total": total,
        "score_mean": total / len(scores) if scores else 0.0,
        "score_min": min(scores) if scores else 0.0,
        "score_max": max(scores) if scores else 0.0,
    }
    snapshot = state.class_state_snapshot
    return ClassReport(
        class_id=snapshot.class_id,
        coverage_rate=snapshot.coverage_rate,
        concept_summaries=[
            item.model_copy(deep=True) for item in snapshot.concept_status
        ],
        misconception_summaries=[
            item.model_copy(deep=True)
            for item in snapshot.misconception_summary
        ],
        score_statistics=statistics,
        evidence_status=snapshot.evidence_status,
    )


def build_individual_report(
    scoring: ScoringResultBundle,
    state: StateUpdateResult,
    *,
    weak_mastery_threshold: float,
    misconception_threshold: float,
) -> IndividualReport:
    """Copy learner mastery and derive display filters without recomputation."""

    learner = state.learner_state_snapshot
    weak_ids = [
        concept.concept_id
        for concept in learner.concept_states
        if concept.mastery_probability < weak_mastery_threshold
    ]
    misconception_ids = list(
        dict.fromkeys(
            item.misconception_id
            for concept in learner.concept_states
            for item in concept.misconceptions
            if item.strength >= misconception_threshold
        )
    )
    audits = latest_audits(scoring)
    return IndividualReport(
        learner_id=learner.learner_id,
        overall_mastery=learner.overall_mastery,
        weak_concept_ids=weak_ids,
        active_misconception_ids=misconception_ids,
        recent_score=scoring.total_score,
        review_required_count=sum(record.needs_review() for record in audits),
    )
