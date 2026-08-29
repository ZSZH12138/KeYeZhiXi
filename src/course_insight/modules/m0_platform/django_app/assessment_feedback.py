"""Per-question feedback projected from one frozen teacher release."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ItemInstance,
    ScoringResultBundle,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app.models import (
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    ReleaseQuestion,
    ReleaseQuestionConceptLink,
    TeacherItemReviewNote,
)


@dataclass(frozen=True, slots=True)
class FeedbackSourceBlock:
    file_name: str
    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class QuestionFeedbackDetail:
    item_instance_id: str
    stem: str
    options: tuple[tuple[str, str], ...]
    concept_names: tuple[str, ...]
    correct_answer: str
    source_blocks: tuple[FeedbackSourceBlock, ...]
    qa_prompt: str
    student_answer: str = ""
    student_score: float | None = None
    max_score: float | None = None
    requires_ai_assessment: bool = False
    ai_assessment: str = ""
    ai_confidence: float | None = None
    teacher_note: str = ""
    qa_token: str = ""


def question_feedback_details(
    *,
    task: TaskPlan,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle | None = None,
    answers: Mapping[str, object] | None = None,
    teacher_notes: Mapping[str, str] | None = None,
) -> tuple[QuestionFeedbackDetail, ...]:
    """Load answers and active knowledge sources without exposing question files."""

    try:
        release = CourseKnowledgeRelease.objects.filter(
            pk=task.knowledge_bundle_id,
            course_id=task.course_id,
            class_id=task.class_id,
            status__in=(
                CourseKnowledgeRelease.Status.ACTIVE,
                CourseKnowledgeRelease.Status.RETIRED,
            ),
        ).first()
    except (ValidationError, ValueError):
        release = None
    if release is None:
        return ()
    questions = {
        question.question_id: question
        for question in ReleaseQuestion.objects.filter(
            release=release,
            question_id__in=tuple(item.item_id for item in paper.all_items()),
        ).prefetch_related(
            "concept_links__concept__source_references__source_version__source"
        )
    }
    details: list[QuestionFeedbackDetail] = []
    audits = _latest_audits_by_item(scoring)
    audit_history = _audit_history_by_item(scoring)
    answer_values = answers or {}
    notes = teacher_notes or {}
    for item in paper.all_items():
        question = questions.get(item.item_id)
        if question is None:
            continue
        links = tuple(
            link
            for link in question.concept_links.all()
            if link.status == ReleaseQuestionConceptLink.Status.USABLE
        )
        names = tuple(sorted({link.concept.name for link in links}))
        blocks: dict[tuple[str, str, str], FeedbackSourceBlock] = {}
        for link in links:
            for reference in link.concept.source_references.all():
                version = reference.source_version
                source = version.source
                if not (
                    source.course_id == task.course_id
                    and source.class_id == task.class_id
                    and source.source_type == CourseSource.SourceType.KNOWLEDGE
                    and source.status == CourseSource.Status.ACTIVE
                    and version.status == CourseSourceVersion.Status.ACTIVE
                ):
                    continue
                key = (source.display_name, reference.locator, reference.chunk_text)
                blocks[key] = FeedbackSourceBlock(
                    file_name=source.display_name,
                    locator=reference.locator,
                    text=reference.chunk_text,
                )
        answer = _correct_answer(question.payload)
        options = _question_options(item, question.payload)
        option_prompt = (
            f"；选项：{'；'.join(f'{label}. {text}' for label, text in options)}"
            if options
            else ""
        )
        prompt = (
            f"请检索当前课程原文并分析这道题。题目：{item.stem}{option_prompt}；"
            f"正确答案：{answer}；涉及知识点：{'、'.join(names)}。"
        )[:2_000]
        requires_ai = (
            item.rubric_id is not None
            or question.question_type
            in {"fill_blank", "short_answer", "essay", "subjective"}
        )
        ai_assessment, ai_confidence = _ai_assessment(
            requires_ai=requires_ai,
            audits=audit_history.get(item.item_instance_id, ()),
        )
        details.append(
            QuestionFeedbackDetail(
                item_instance_id=item.item_instance_id,
                stem=item.stem,
                options=options,
                concept_names=names,
                correct_answer=answer,
                student_answer=_student_answer(
                    answer_values.get(item.item_instance_id)
                ),
                student_score=(
                    None
                    if audits.get(item.item_instance_id) is None
                    else audits[item.item_instance_id].total_score
                ),
                max_score=(
                    item.max_score
                    if audits.get(item.item_instance_id) is None
                    else audits[item.item_instance_id].max_score
                ),
                requires_ai_assessment=requires_ai,
                ai_assessment=ai_assessment,
                ai_confidence=ai_confidence,
                teacher_note=str(
                    notes.get(item.item_instance_id, "")
                ).strip(),
                source_blocks=tuple(blocks[key] for key in sorted(blocks)),
                qa_prompt=prompt,
            )
        )
    return tuple(details)


def _correct_answer(payload: object) -> str:
    if not isinstance(payload, dict):
        return "教师未提供参考答案。"
    for key in ("answers", "accepted_answers"):
        answers = payload.get(key)
        if isinstance(answers, list):
            values = tuple(
                str(value).strip()
                for value in answers
                if isinstance(value, str) and value.strip()
            )
            if values:
                return "；".join(values)
    answer = payload.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    rubric = payload.get("rubric")
    if isinstance(rubric, str) and rubric.strip():
        return rubric.strip()
    return "教师未提供参考答案。"


def _question_options(
    item: ItemInstance,
    payload: object,
) -> tuple[tuple[str, str], ...]:
    frozen = item.parameters.get("_choice_options")
    raw_options = frozen if isinstance(frozen, dict) and frozen else None
    if raw_options is None and isinstance(payload, dict):
        candidate = payload.get("options")
        raw_options = candidate if isinstance(candidate, dict) else None
    if raw_options is None:
        return ()
    options = (
        (str(label).strip(), str(text).strip())
        for label, text in raw_options.items()
    )
    return tuple(
        sorted(
            ((label, text) for label, text in options if label and text),
            key=lambda option: option[0],
        )
    )


def _latest_audits_by_item(
    scoring: ScoringResultBundle | None,
) -> dict[str, object]:
    if scoring is None:
        return {}
    latest: dict[str, object] = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.item_instance_id)
        if current is None or audit.audit_version > current.audit_version:
            latest[audit.item_instance_id] = audit
    return latest


def _audit_history_by_item(
    scoring: ScoringResultBundle | None,
) -> dict[str, tuple[object, ...]]:
    if scoring is None:
        return {}
    grouped: dict[str, list[object]] = {}
    for audit in scoring.score_audit_records:
        grouped.setdefault(audit.item_instance_id, []).append(audit)
    return {
        item_instance_id: tuple(
            sorted(values, key=lambda audit: audit.audit_version)
        )
        for item_instance_id, values in grouped.items()
    }


def _ai_assessment(
    *,
    requires_ai: bool,
    audits: tuple[object, ...],
) -> tuple[str, float | None]:
    if not requires_ai or not audits:
        return "", None
    model_audit = next(
        (
            audit
            for audit in reversed(audits)
            if audit.scoring_method in {"local_model", "local_model_rescore"}
        ),
        None,
    )
    if model_audit is None:
        rule_audit = next(
            (
                audit
                for audit in reversed(audits)
                if audit.scoring_method == "rule"
            ),
            None,
        )
        return ("无", 1.0) if rule_audit is not None else ("", None)
    reasons = tuple(
        dict.fromkeys(
            criterion.reason.strip()
            for criterion in model_audit.criterion_scores
            if criterion.reason.strip()
        )
    )
    assessment = "；".join(reasons) or "AI未提供评分原因。"
    confidence = float(model_audit.confidence)
    warning = "ai评分置信度不足 建议通知相应教师进行重新评分"
    if confidence < 0.5 and warning not in assessment:
        assessment = f"{assessment.rstrip('。；')}。{warning}"
    return assessment, confidence


def _student_answer(value: object) -> str:
    if value is None:
        return "未提交答案"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)):
        rendered = str(value).strip()
        return rendered or "未提交答案"
    return "未提交答案"


def teacher_notes_for_attempt(attempt_id: str) -> dict[str, str]:
    """Return only the most recent teacher note for each reviewed item."""

    latest: dict[str, TeacherItemReviewNote] = {}
    for note in TeacherItemReviewNote.objects.filter(
        attempt_id=attempt_id
    ).order_by("item_instance_id", "-audit_version", "-pk"):
        latest.setdefault(note.item_instance_id, note)
    return {
        item_instance_id: note.teacher_note
        for item_instance_id, note in latest.items()
    }


__all__ = [
    "FeedbackSourceBlock",
    "QuestionFeedbackDetail",
    "question_feedback_details",
    "teacher_notes_for_attempt",
]
