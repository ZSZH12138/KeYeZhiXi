"""Frozen presentation-only projections of existing public contracts."""

from __future__ import annotations

from dataclasses import dataclass

from course_insight.contracts.analytics import TeacherAnalyticsBundle
from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.tutoring import StudentFeedbackPackage


@dataclass(frozen=True, slots=True)
class ItemView:
    item_instance_id: str
    stem: str
    max_score: float


@dataclass(frozen=True, slots=True)
class SectionView:
    name: str
    score: float
    items: tuple[ItemView, ...]


@dataclass(frozen=True, slots=True)
class PaperView:
    paper_id: str
    total_score: float
    sections: tuple[SectionView, ...]


@dataclass(frozen=True, slots=True)
class CriterionView:
    criterion_id: str
    score: float
    student_evidence: str
    course_evidence_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class AuditView:
    audit_id: str
    audit_version: int
    item_instance_id: str
    total_score: float
    max_score: float
    confidence: float
    review_status: str
    review_reasons: tuple[str, ...]
    criteria: tuple[CriterionView, ...]


@dataclass(frozen=True, slots=True)
class RubricFeedbackView:
    criterion_id: str
    earned_score: float
    max_score: float
    message: str
    student_evidence: str


@dataclass(frozen=True, slots=True)
class CitationView:
    label: str
    quote: str


@dataclass(frozen=True, slots=True)
class FeedbackView:
    message: str
    rubric_feedback: tuple[RubricFeedbackView, ...]
    missing_concept_ids: tuple[str, ...]
    citations: tuple[CitationView, ...]
    next_practice_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StudentResultView:
    paper: PaperView
    attempt_id: str
    total_score: float | None
    max_score: float
    audits: tuple[AuditView, ...]
    feedback: FeedbackView | None
    score_pending_rescore: bool


@dataclass(frozen=True, slots=True)
class IndividualReportView:
    learner_id: str
    overall_mastery: float
    recent_score: float
    weak_concept_ids: tuple[str, ...]
    active_misconception_ids: tuple[str, ...]
    review_required_count: int


@dataclass(frozen=True, slots=True)
class SuggestionView:
    content: str
    concept_ids: tuple[str, ...]
    affected_count: int
    coverage_rate: float
    confidence: float
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalyticsView:
    class_id: str
    coverage_rate: float
    evidence_status: str
    score_statistics: tuple[tuple[str, float], ...]
    individual_reports: tuple[IndividualReportView, ...]
    review_count: int
    suggestions: tuple[SuggestionView, ...]


@dataclass(frozen=True, slots=True)
class TeacherReviewView:
    paper: PaperView
    audit: AuditView
    analytics: AnalyticsView | None


def paper_view(paper: AssessmentPaper) -> PaperView:
    return PaperView(
        paper_id=paper.paper_id,
        total_score=paper.total_score(),
        sections=tuple(
            SectionView(
                name=section.name,
                score=section.score,
                items=tuple(
                    ItemView(
                        item_instance_id=item.item_instance_id,
                        stem=item.stem,
                        max_score=item.max_score,
                    )
                    for item in section.items
                ),
            )
            for section in paper.sections
        ),
    )


def audit_view(audit: ScoreAuditRecord) -> AuditView:
    return AuditView(
        audit_id=audit.audit_id,
        audit_version=audit.audit_version,
        item_instance_id=audit.item_instance_id,
        total_score=audit.total_score,
        max_score=audit.max_score,
        confidence=audit.confidence,
        review_status=audit.review_status,
        review_reasons=tuple(audit.review_reason),
        criteria=tuple(
            CriterionView(
                criterion_id=item.criterion_id,
                score=item.score,
                student_evidence=item.student_evidence,
                course_evidence_id=item.course_evidence_id,
                reason=item.reason,
            )
            for item in audit.criterion_scores
        ),
    )


def feedback_view(feedback: StudentFeedbackPackage) -> FeedbackView:
    return FeedbackView(
        message=feedback.message,
        rubric_feedback=tuple(
            RubricFeedbackView(
                criterion_id=item.criterion_id,
                earned_score=item.earned_score,
                max_score=item.max_score,
                message=item.message,
                student_evidence=item.student_evidence,
            )
            for item in feedback.rubric_feedback
        ),
        missing_concept_ids=tuple(feedback.missing_concept_ids),
        citations=tuple(
            CitationView(
                label=citation.label(),
                quote=citation.quote,
            )
            for citation in feedback.evidence_citations
        ),
        next_practice_item_ids=tuple(feedback.next_practice_item_ids),
    )


def student_result_view(
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
    feedback: StudentFeedbackPackage,
) -> StudentResultView:
    hidden = scoring.requires_teacher_review() or scoring.has_rejected_score()
    return StudentResultView(
        paper=paper_view(paper),
        attempt_id=scoring.attempt_id,
        total_score=None if hidden else scoring.total_score,
        max_score=scoring.max_score,
        audits=() if hidden else tuple(
            audit_view(item) for item in _current_audits(scoring)
        ),
        feedback=None if hidden else feedback_view(feedback),
        score_pending_rescore=hidden,
    )


def analytics_view(analytics: TeacherAnalyticsBundle) -> AnalyticsView:
    report = analytics.class_report
    return AnalyticsView(
        class_id=report.class_id,
        coverage_rate=report.coverage_rate,
        evidence_status=report.evidence_status,
        score_statistics=tuple(sorted(report.score_statistics.items())),
        individual_reports=tuple(
            IndividualReportView(
                learner_id=item.learner_id,
                overall_mastery=item.overall_mastery,
                recent_score=item.recent_score,
                weak_concept_ids=tuple(item.weak_concept_ids),
                active_misconception_ids=tuple(
                    item.active_misconception_ids
                ),
                review_required_count=item.review_required_count,
            )
            for item in analytics.individual_reports
        ),
        review_count=analytics.open_review_count(),
        suggestions=tuple(
            SuggestionView(
                content=item.content,
                concept_ids=tuple(item.concept_ids),
                affected_count=item.affected_count,
                coverage_rate=item.coverage_rate,
                confidence=item.confidence,
                evidence_ids=tuple(item.evidence_ids),
            )
            for item in analytics.teaching_suggestions
        ),
    )


def teacher_review_view(
    paper: AssessmentPaper,
    audit: ScoreAuditRecord,
    analytics: TeacherAnalyticsBundle | None,
) -> TeacherReviewView:
    return TeacherReviewView(
        paper=paper_view(paper),
        audit=audit_view(audit),
        analytics=None if analytics is None else analytics_view(analytics),
    )


def criterion_caps(
    *,
    paper: AssessmentPaper,
    audit: ScoreAuditRecord,
    knowledge_bundle: KnowledgeBundle,
) -> dict[str, float]:
    """Resolve score caps only through supplied authoritative contracts."""

    item = paper.get_item_instance(audit.item_instance_id)
    audit_ids = {
        criterion.criterion_id for criterion in audit.criterion_scores
    }
    if item.rubric_id is None:
        if len(audit_ids) != 1:
            raise ValueError("objective audit criteria are invalid")
        return {next(iter(audit_ids)): item.max_score}
    rubric = knowledge_bundle.get_rubric(item.rubric_id)
    caps = {
        criterion.criterion_id: criterion.max_score
        for criterion in rubric.criteria
    }
    if set(caps) != audit_ids:
        raise ValueError("audit and rubric criteria do not match")
    return caps


def _current_audits(
    scoring: ScoringResultBundle,
) -> tuple[ScoreAuditRecord, ...]:
    latest: dict[str, ScoreAuditRecord] = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.audit_id)
        if current is None or audit.audit_version > current.audit_version:
            latest[audit.audit_id] = audit
    return tuple(latest[key] for key in sorted(latest))
