"""Read-only student history for assessments that changed mastery profiles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
    User,
)


PROFILE_AFFECTING_TASK_TYPES = ("diagnostic", "stage_assessment")
_TASK_LABELS = {
    "diagnostic": "诊断测评",
    "stage_assessment": "阶段评测",
}
_HISTORY_LIMIT = 50


@dataclass(frozen=True, slots=True)
class ProfileAssessmentHistoryEntry:
    paper_id: str
    task_type: str
    task_label: str
    completed_at: datetime
    total_score: float | None
    max_score: float | None
    wrong_count: int | None
    result_url: str = ""

    @property
    def result_available(self) -> bool:
        return self.total_score is not None and self.max_score is not None


def load_profile_assessment_history(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    coordinator: object,
) -> tuple[ProfileAssessmentHistoryEntry, ...]:
    """Load only this learner's finalized, profile-affecting scoped papers."""

    receipts = AssessmentProjectionReceipt.objects.filter(
        workspace__course_id=course_id,
        workspace__class_id=class_id,
        learner=learner,
        task_type__in=PROFILE_AFFECTING_TASK_TYPES,
    ).order_by("-applied_at", "-pk")[:_HISTORY_LIMIT]
    entries: list[ProfileAssessmentHistoryEntry] = []
    for receipt in receipts:
        scoring = _load_scoring(
            coordinator,
            paper_id=receipt.paper_id,
            learner_id=learner.actor_id,
        )
        entries.append(
            ProfileAssessmentHistoryEntry(
                paper_id=receipt.paper_id,
                task_type=receipt.task_type,
                task_label=_TASK_LABELS[receipt.task_type],
                completed_at=receipt.applied_at,
                total_score=None if scoring is None else scoring.total_score,
                max_score=None if scoring is None else scoring.max_score,
                wrong_count=None if scoring is None else _wrong_count(scoring),
            )
        )
    return tuple(entries)


def _load_scoring(
    coordinator: object,
    *,
    paper_id: str,
    learner_id: str,
) -> ScoringResultBundle | None:
    loader = getattr(coordinator, "get_student_assessment", None)
    if not callable(loader):
        return None
    try:
        response = loader(paper_id=paper_id, learner_id=learner_id)
    except DomainError:
        return None
    if not isinstance(response, dict):
        return None
    scoring = response.get("scoring_result")
    return scoring if type(scoring) is ScoringResultBundle else None


def _wrong_count(scoring: ScoringResultBundle) -> int:
    latest = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.item_instance_id)
        if current is None or audit.audit_version > current.audit_version:
            latest[audit.item_instance_id] = audit
    return sum(
        1
        for audit in latest.values()
        if not math.isclose(
            audit.total_score,
            audit.max_score,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )


__all__ = [
    "PROFILE_AFFECTING_TASK_TYPES",
    "ProfileAssessmentHistoryEntry",
    "load_profile_assessment_history",
]
