"""Frozen presentation-only projections of existing public contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from course_insight.contracts.analytics import TeacherAnalyticsBundle
from course_insight.modules.m0_platform.correction_records import (
    class_correction_rates,
    correction_status,
)
from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.state import LearnerStateSnapshot
from course_insight.contracts.tutoring import StudentFeedbackPackage

_PURPOSE_LABELS = {
    "anchor": "基准",
    "uncertainty": "不确定点",
    "misconception": "误区",
    "remediation": "补救",
}
_FOUR_PURPOSES = ("anchor", "uncertainty", "misconception", "remediation")


@dataclass(frozen=True, slots=True)
class ItemView:
    item_instance_id: str
    stem: str
    max_score: float


@dataclass(frozen=True, slots=True)
class SectionView:
    name: str
    purpose: str | None
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
    scoring_method: str
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
    correction_note: str
    item_notes: tuple["ItemCorrectionNoteView", ...]


@dataclass(frozen=True, slots=True)
class ConceptProfileView:
    concept_id: str
    name: str
    band: str
    misconceptions: tuple[str, ...]
    evidence_count: int
    source_locator: str
    mastery: float = 0.0
    attempted_count: int = 0
    correct_count: int = 0
    attempt_status: str = "unseen"


@dataclass(frozen=True, slots=True)
class StudentProfileView:
    ready: bool
    message: str
    mastered: tuple[ConceptProfileView, ...]
    consolidating: tuple[ConceptProfileView, ...]
    priority_support: tuple[ConceptProfileView, ...]
    next_practice_concept_ids: tuple[str, ...]
    next_practice_names: tuple[str, ...]
    progress: tuple[tuple[int, float], ...]
    unseen: tuple[ConceptProfileView, ...] = ()


@dataclass(frozen=True, slots=True)
class LostItemGuideView:
    item_instance_id: str
    stem: str
    cause: str
    concept_ids: tuple[str, ...]
    concept_names: tuple[str, ...]
    status: str


@dataclass(frozen=True, slots=True)
class CorrectionGuideView:
    available: bool
    hint: str
    hint_revealed: bool
    lost_items: tuple[LostItemGuideView, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class ItemCorrectionNoteView:
    item_instance_id: str
    status: str


@dataclass(frozen=True, slots=True)
class StudentResultView:
    paper: PaperView
    attempt_id: str
    total_score: float | None
    max_score: float
    audits: tuple[AuditView, ...]
    feedback: FeedbackView | None
    score_pending_rescore: bool
    correction_available: bool


@dataclass(frozen=True, slots=True)
class IndividualReportView:
    learner_id: str
    overall_mastery: float
    recent_score: float
    weak_concept_ids: tuple[str, ...]
    active_misconception_ids: tuple[str, ...]
    review_required_count: int
    hint_dependency: float | None
    recent_correction_rate: float | None


@dataclass(frozen=True, slots=True)
class SuggestionView:
    suggestion_id: str
    status: str
    content: str
    concept_ids: tuple[str, ...]
    affected_count: int
    coverage_rate: float
    confidence: float
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConceptSummaryView:
    concept_id: str
    mean_mastery: float
    priority_support: float
    sample_count: int
    trend_delta: float | None


@dataclass(frozen=True, slots=True)
class ChapterSummaryView:
    chapter_id: str
    mean_mastery: float
    concept_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MisconceptionSummaryView:
    misconception_id: str
    affected_count: int
    affected_rate: float


@dataclass(frozen=True, slots=True)
class AnalyticsView:
    report_id: str
    class_id: str
    coverage_rate: float
    evidence_status: str
    score_statistics: tuple[tuple[str, float], ...]
    concept_summaries: tuple[ConceptSummaryView, ...]
    misconception_summaries: tuple[MisconceptionSummaryView, ...]
    individual_reports: tuple[IndividualReportView, ...]
    review_count: int
    suggestions: tuple[SuggestionView, ...]
    chapters: tuple[ChapterSummaryView, ...]
    correction_completion_rate: float | None
    post_hint_correction_rate: float | None


@dataclass(frozen=True, slots=True)
class TeacherReviewView:
    paper: PaperView
    audit: AuditView
    analytics: AnalyticsView | None


@dataclass(frozen=True, slots=True)
class LearnerLostItemView:
    item_instance_id: str
    stem: str
    cause: str
    status: str


@dataclass(frozen=True, slots=True)
class LearnerReviewView:
    learner_id: str
    profile: StudentProfileView
    lost_items: tuple[LearnerLostItemView, ...]


def paper_view(
    paper: AssessmentPaper,
    *,
    section_purposes: Mapping[str, str] | None = None,
) -> PaperView:
    purposes = section_purposes or {}
    return PaperView(
        paper_id=paper.paper_id,
        total_score=paper.total_score(),
        sections=tuple(
            SectionView(
                name=section.name,
                purpose=_PURPOSE_LABELS.get(
                    purposes.get(section.section_id, ""),
                    purposes.get(section.section_id),
                ),
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
        scoring_method=audit.scoring_method,
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


def feedback_view(
    feedback: StudentFeedbackPackage,
    *,
    correction_note: str = "尚未订正",
    item_notes: Sequence[ItemCorrectionNoteView] = (),
) -> FeedbackView:
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
        correction_note=correction_note,
        item_notes=tuple(item_notes),
    )


def student_result_view(
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
    feedback: StudentFeedbackPackage,
) -> StudentResultView:
    hidden = scoring.requires_teacher_review() or scoring.has_rejected_score()
    lost_points = scoring.total_score < scoring.max_score
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
        correction_available=not hidden and lost_points,
    )


def analytics_view(
    analytics: TeacherAnalyticsBundle,
    *,
    knowledge_bundle: KnowledgeBundle | None = None,
    learner_states: Sequence[LearnerStateSnapshot] = (),
    records: Mapping[str, Any] | None = None,
    course_id: str | None = None,
) -> AnalyticsView:
    report = analytics.class_report
    states_by_learner = {
        snapshot.learner_id: snapshot for snapshot in learner_states
    }
    concept_states = [
        state
        for snapshot in learner_states
        for state in snapshot.concept_states
    ]
    completion = None
    post_hint = None
    if concept_states:
        completion = sum(
            1 for state in concept_states if state.recent_correction_rate > 0.0
        ) / len(concept_states)
        hinted = [state for state in concept_states if state.hint_dependency > 0.0]
        if hinted:
            post_hint = sum(
                1 for state in hinted if state.recent_correction_rate > 0.0
            ) / len(hinted)
    scoped_course = course_id
    if scoped_course is None and knowledge_bundle is not None:
        scoped_course = knowledge_bundle.course_id
    if records and scoped_course:
        recorded_completion, recorded_post_hint = class_correction_rates(
            records,
            course_id=scoped_course,
            class_id=report.class_id,
        )
        if recorded_completion is not None:
            completion = recorded_completion
            post_hint = recorded_post_hint
    mastery_by_concept = {
        item.concept_id: item.mean_mastery_probability
        for item in report.concept_summaries
    }
    chapters: list[ChapterSummaryView] = []
    if knowledge_bundle is not None:
        grouped: dict[str, list[str]] = {}
        for concept in knowledge_bundle.concepts:
            grouped.setdefault(concept.chapter_id, []).append(concept.concept_id)
        for chapter_id, concept_ids in grouped.items():
            values = [
                mastery_by_concept[concept_id]
                for concept_id in concept_ids
                if concept_id in mastery_by_concept
            ]
            chapters.append(
                ChapterSummaryView(
                    chapter_id=chapter_id,
                    mean_mastery=(
                        sum(values) / len(values) if values else 0.0
                    ),
                    concept_ids=tuple(concept_ids),
                )
            )
    return AnalyticsView(
        report_id=analytics.report_id,
        class_id=report.class_id,
        coverage_rate=report.coverage_rate,
        evidence_status=report.evidence_status,
        score_statistics=tuple(sorted(report.score_statistics.items())),
        concept_summaries=tuple(
            ConceptSummaryView(
                concept_id=item.concept_id,
                mean_mastery=item.mean_mastery_probability,
                priority_support=item.mastery_distribution.priority_support,
                sample_count=item.sample_count,
                trend_delta=item.mastery_trend_delta,
            )
            for item in report.concept_summaries
        ),
        misconception_summaries=tuple(
            MisconceptionSummaryView(
                misconception_id=item.misconception_id,
                affected_count=item.affected_count,
                affected_rate=item.affected_rate_among_assessed,
            )
            for item in report.misconception_summaries
        ),
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
                hint_dependency=_mean_state_metric(
                    states_by_learner.get(item.learner_id),
                    "hint_dependency",
                ),
                recent_correction_rate=_mean_state_metric(
                    states_by_learner.get(item.learner_id),
                    "recent_correction_rate",
                ),
            )
            for item in analytics.individual_reports
        ),
        review_count=analytics.open_review_count(),
        suggestions=tuple(
            SuggestionView(
                suggestion_id=item.suggestion_id,
                status=item.status,
                content=item.content,
                concept_ids=tuple(item.concept_ids),
                affected_count=item.affected_count,
                coverage_rate=item.coverage_rate,
                confidence=item.confidence,
                evidence_ids=tuple(item.evidence_ids),
            )
            for item in analytics.teaching_suggestions
        ),
        chapters=tuple(chapters),
        correction_completion_rate=completion,
        post_hint_correction_rate=post_hint,
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
        # A subjective question with teacher-supplied reference answers may
        # have been rule-scored before the rubric path was introduced.  Such
        # an audit has one authoritative item-level criterion, whose cap is
        # the frozen audit maximum rather than the current rubric criterion.
        # Keep it rejudgeable without rewriting its historical audit record.
        if len(audit_ids) == 1:
            return {next(iter(audit_ids)): audit.max_score}
        raise ValueError("audit and rubric criteria do not match")
    return caps


def student_profile_view(
    *,
    snapshot: LearnerStateSnapshot | None,
    knowledge_bundle: KnowledgeBundle,
    mastered_threshold: float,
    consolidating_threshold: float,
    misconception_activation_threshold: float,
    progress: tuple[tuple[int, float], ...] = (),
) -> StudentProfileView:
    """Project confirmed learner state into three mastery lists."""

    empty = StudentProfileView(
        ready=False,
        message="还没有已确认的测评，确认后才会更新掌握画像。",
        mastered=(),
        consolidating=(),
        priority_support=(),
        next_practice_concept_ids=(),
        next_practice_names=(),
        progress=(),
    )
    if snapshot is None or snapshot.evidence_count <= 0:
        return empty
    names = {concept.concept_id: concept.name for concept in knowledge_bundle.concepts}
    locators = {
        concept.concept_id: concept.chapter_id for concept in knowledge_bundle.concepts
    }
    misconception_names = {
        tag.misconception_id: tag.name for tag in knowledge_bundle.misconception_tags
    }
    grouped: dict[str, list[ConceptProfileView]] = {
        "mastered": [],
        "consolidating": [],
        "priority_support": [],
    }
    for state in snapshot.concept_states:
        if state.evidence_count <= 0:
            continue
        band = state.band(mastered_threshold, consolidating_threshold)
        active = tuple(
            misconception_names.get(item.misconception_id, item.misconception_id)
            for item in state.active_misconceptions(
                misconception_activation_threshold
            )
        )
        grouped[band].append(
            ConceptProfileView(
                concept_id=state.concept_id,
                name=names.get(state.concept_id, state.concept_id),
                band=band,
                misconceptions=active,
                evidence_count=state.evidence_count,
                source_locator=locators.get(state.concept_id, ""),
            )
        )
    priority = tuple(grouped["priority_support"])
    practice_ids = tuple(item.concept_id for item in priority)
    return StudentProfileView(
        ready=True,
        message="以下状态只包含老师确认后的测评。",
        mastered=tuple(grouped["mastered"]),
        consolidating=tuple(grouped["consolidating"]),
        priority_support=priority,
        next_practice_concept_ids=practice_ids,
        next_practice_names=tuple(
            names.get(concept_id, concept_id) for concept_id in practice_ids
        ),
        progress=progress,
    )


def count_mastery_profile_view(
    *,
    mastery_records: Sequence[object],
    knowledge_bundle: KnowledgeBundle,
    mastered_threshold: float = 0.8,
    consolidating_threshold: float = 0.4,
) -> StudentProfileView:
    """Project the authoritative correct/attempted counters for students."""

    by_concept = {
        str(getattr(record, "concept_id")): record for record in mastery_records
    }
    grouped: dict[str, list[ConceptProfileView]] = {
        "mastered": [],
        "consolidating": [],
        "priority_support": [],
    }
    for concept in knowledge_bundle.concepts:
        record = by_concept.get(concept.concept_id)
        attempted = int(getattr(record, "attempted_count", 0))
        # The student profile is an evidence view, not the complete syllabus:
        # unseen concepts must neither appear nor be interpreted as weak.
        if attempted <= 0:
            continue
        correct = int(getattr(record, "correct_count", 0))
        mastery = float(getattr(record, "mastery", 0.0))
        if mastery >= mastered_threshold:
            band = "mastered"
        elif mastery >= consolidating_threshold:
            band = "consolidating"
        else:
            band = "priority_support"
        grouped[band].append(
            ConceptProfileView(
                concept_id=concept.concept_id,
                name=concept.name,
                band=band,
                misconceptions=(),
                evidence_count=attempted,
                source_locator=concept.chapter_id,
                mastery=mastery,
                attempted_count=attempted,
                correct_count=correct,
                attempt_status="unseen" if attempted == 0 else "attempted",
            )
        )
    ready = any(
        int(getattr(record, "attempted_count", 0)) > 0
        for record in by_concept.values()
    )
    priority = tuple(grouped["priority_support"])
    return StudentProfileView(
        ready=ready,
        message=(
            "掌握度仅由诊断测评和阶段评测更新，最高为 0.9。"
            if ready
            else "还没有计入画像的作答。"
        ),
        mastered=tuple(grouped["mastered"]),
        consolidating=tuple(grouped["consolidating"]),
        priority_support=priority,
        next_practice_concept_ids=tuple(item.concept_id for item in priority),
        next_practice_names=tuple(item.name for item in priority),
        progress=(),
        unseen=(),
    )


def blueprint_section_purposes(
    knowledge_bundle: KnowledgeBundle,
    blueprint_id: str,
) -> dict[str, str]:
    try:
        blueprint = knowledge_bundle.get_blueprint(blueprint_id)
    except (DomainError, KeyError, ValueError):
        return {}
    count = len(blueprint.sections)
    purposes: dict[str, str] = {}
    for index, section in enumerate(blueprint.sections):
        purpose = section.purpose
        if purpose is None and count == 4:
            purpose = _FOUR_PURPOSES[index]
        if purpose:
            purposes[section.section_id] = purpose
    return purposes


def correction_guide_view(
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
    knowledge_bundle: KnowledgeBundle,
    *,
    hint_revealed: bool = False,
    records: Mapping[str, Any] | None = None,
) -> CorrectionGuideView:
    names = {
        concept.concept_id: concept.name for concept in knowledge_bundle.concepts
    }
    stored: Mapping[str, Any] = {}
    if isinstance(records, Mapping):
        payload = records.get(paper.paper_id)
        if isinstance(payload, Mapping):
            items = payload.get("items")
            if isinstance(items, Mapping):
                stored = items
    hidden = scoring.requires_teacher_review() or scoring.has_rejected_score()
    lost: list[LostItemGuideView] = []
    if not hidden:
        for audit in _current_audits(scoring):
            if audit.total_score >= audit.max_score:
                continue
            instance = paper.get_item_instance(audit.item_instance_id)
            reasons = [
                criterion.reason
                for criterion in audit.criterion_scores
                if criterion.reason
            ]
            concept_ids = tuple(instance.concept_ids)
            item_record = stored.get(instance.item_instance_id)
            lost.append(
                LostItemGuideView(
                    item_instance_id=instance.item_instance_id,
                    stem=instance.stem,
                    cause="；".join(reasons) if reasons else "本题未得到满分。",
                    concept_ids=concept_ids,
                    concept_names=tuple(
                        names.get(concept_id, concept_id)
                        for concept_id in concept_ids
                    ),
                    status=(
                        correction_status(item_record)
                        if isinstance(item_record, Mapping)
                        else "尚未订正"
                    ),
                )
            )
    hint = (
        "请对照题目要求核对书写与大小写，不要直接看标准答案。"
        if hint_revealed
        else "先根据错因自己改一遍。需要时再展开一层最小提示。"
    )
    return CorrectionGuideView(
        available=bool(lost),
        hint=hint,
        hint_revealed=hint_revealed,
        lost_items=tuple(lost),
        summary="订正完成后再做跟练。未独立订正不会记成已经掌握。",
    )


def item_correction_notes(
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
    records: Mapping[str, Any] | None = None,
) -> tuple[ItemCorrectionNoteView, ...]:
    stored = {}
    if isinstance(records, Mapping):
        payload = records.get(paper.paper_id)
        if isinstance(payload, Mapping):
            items = payload.get("items")
            if isinstance(items, Mapping):
                stored = items
    notes: list[ItemCorrectionNoteView] = []
    for audit in _current_audits(scoring):
        if audit.total_score >= audit.max_score:
            continue
        instance = paper.get_item_instance(audit.item_instance_id)
        item = stored.get(instance.item_instance_id)
        status = (
            correction_status(item)
            if isinstance(item, Mapping)
            else "尚未订正"
        )
        notes.append(
            ItemCorrectionNoteView(
                item_instance_id=instance.item_instance_id,
                status=status,
            )
        )
    return tuple(notes)


def learner_review_view(
    *,
    learner_id: str,
    knowledge_bundle: KnowledgeBundle,
    snapshot: LearnerStateSnapshot | None,
    records: Mapping[str, Any],
    paper: AssessmentPaper | None = None,
    scoring: ScoringResultBundle | None = None,
    mastered_threshold: float = 0.8,
    consolidating_threshold: float = 0.4,
    misconception_activation_threshold: float = 0.5,
    authoritative_mastery: Sequence[object] | None = None,
) -> LearnerReviewView:
    del paper, scoring
    lost: list[LearnerLostItemView] = []
    for payload in records.values():
        if not isinstance(payload, Mapping) or payload.get("learner_id") != learner_id:
            continue
        items = payload.get("items", {})
        if not isinstance(items, Mapping):
            continue
        for instance_id, item in items.items():
            if not isinstance(item, Mapping):
                continue
            lost.append(
                LearnerLostItemView(
                    item_instance_id=str(instance_id),
                    stem=str(item.get("stem") or ""),
                    cause=str(item.get("cause") or ""),
                    status=correction_status(item),
                )
            )
    return LearnerReviewView(
        learner_id=learner_id,
        profile=(
            count_mastery_profile_view(
                mastery_records=authoritative_mastery,
                knowledge_bundle=knowledge_bundle,
                mastered_threshold=mastered_threshold,
                consolidating_threshold=consolidating_threshold,
            )
            if authoritative_mastery is not None
            else student_profile_view(
                snapshot=snapshot,
                knowledge_bundle=knowledge_bundle,
                mastered_threshold=mastered_threshold,
                consolidating_threshold=consolidating_threshold,
                misconception_activation_threshold=(
                    misconception_activation_threshold
                ),
            )
        ),
        lost_items=tuple(lost),
    )


def _mean_state_metric(
    snapshot: LearnerStateSnapshot | None,
    field_name: str,
) -> float | None:
    if snapshot is None or not snapshot.concept_states:
        return None
    values = [getattr(state, field_name) for state in snapshot.concept_states]
    return sum(values) / len(values)


def _current_audits(
    scoring: ScoringResultBundle,
) -> tuple[ScoreAuditRecord, ...]:
    latest: dict[str, ScoreAuditRecord] = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.audit_id)
        if current is None or audit.audit_version > current.audit_version:
            latest[audit.audit_id] = audit
    return tuple(latest[key] for key in sorted(latest))
