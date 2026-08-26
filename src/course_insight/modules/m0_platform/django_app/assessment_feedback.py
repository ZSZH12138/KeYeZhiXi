"""Per-question feedback projected from one frozen teacher release."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from course_insight.contracts.assessment import AssessmentPaper, ItemInstance
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app.models import (
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    ReleaseQuestion,
    ReleaseQuestionConceptLink,
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
    qa_token: str = ""


def question_feedback_details(
    *,
    task: TaskPlan,
    paper: AssessmentPaper,
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
        details.append(
            QuestionFeedbackDetail(
                item_instance_id=item.item_instance_id,
                stem=item.stem,
                options=options,
                concept_names=names,
                correct_answer=answer,
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


__all__ = [
    "FeedbackSourceBlock",
    "QuestionFeedbackDetail",
    "question_feedback_details",
]
