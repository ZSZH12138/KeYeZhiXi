"""Independent student QA over one current course/class release."""

from __future__ import annotations

from datetime import datetime, timezone

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from course_insight.contracts.errors import DomainError
from course_insight.contracts.assessment import AssessmentPaper
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m0_platform.django_app.authz import authorize_scope
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.assessment_feedback import (
    question_feedback_details,
)
from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    CourseSource,
    CourseSourceVersion,
    ReleaseConcept,
    ReleaseConceptSource,
    ReleaseQuestionConceptLink,
)
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    ScopedDeepSeekSettings,
    resolve_scoped_deepseek_settings,
    scoped_deepseek_status,
)
from course_insight.modules.m0_platform.django_app.qa_handoff import (
    verify_qa_handoff,
)
from course_insight.modules.m7_local_model.privacy_reviewer import (
    PrivacyReviewResult,
)
from course_insight.modules.m7_local_model.student_qa import (
    DeepSeekStudentQAAdapter,
    StudentQAAnswer,
    StudentQAConcept,
    StudentQAContext,
    StudentQAEvidence,
    StudentQAExample,
)


class StudentQAForm(forms.Form):
    question = forms.CharField(
        label="课程问题",
        min_length=2,
        max_length=2_000,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": "例如：拥塞控制为什么需要慢启动？",
            }
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def page(request: HttpRequest, course_id: str, class_id: str) -> HttpResponse:
    authorize_scope(
        request.user,
        "ask_course_question",
        course_id=course_id,
        class_id=class_id,
        learner_id=request.user.actor_id,
    )
    scoped_settings = resolve_scoped_deepseek_settings(course_id, class_id)
    public_status = scoped_deepseek_status(course_id, class_id)
    answer: StudentQAAnswer | None = None
    error_message: str | None = None
    form_data = request.POST or None
    if request.method == "POST" and request.POST.get("question_ref"):
        try:
            question = _question_from_handoff(
                request,
                token=str(request.POST["question_ref"]),
                course_id=course_id,
                class_id=class_id,
            )
        except DomainError as error:
            error_message = error.message
            form_data = None
        else:
            form_data = request.POST.copy()
            form_data["question"] = question
    form = StudentQAForm(form_data)
    if (
        request.method == "POST"
        and error_message is None
        and form.is_valid()
    ):
        if scoped_settings is None:
            error_message = "教师尚未为当前课程班级配置 DeepSeek API，答疑暂不可用。"
        else:
            release = _active_release(course_id, class_id)
            concepts = _active_concepts(release)
            try:
                adapter = build_student_qa_adapter(scoped_settings)
                answer = adapter.answer(
                    str(form.cleaned_data["question"]),
                    concepts,
                    lambda concept_id: (
                        StudentQAContext(evidence=(), examples=())
                        if release is None
                        else load_active_concept_context(
                            course_id=course_id,
                            class_id=class_id,
                            release_id=str(release.pk),
                            concept_id=concept_id,
                        )
                    ),
                    model_ref=LLMModelRef(
                        model_name=scoped_settings.model_name,
                        model_version="runtime-api",
                        status="configured",
                    ),
                    created_at=datetime.now(timezone.utc),
                )
            except DomainError:
                error_message = (
                    "答疑服务暂时不可用，请稍后重试；课程材料和学习画像不受影响。"
                )
    return render(
        request,
        "course_insight/student/qa.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "form": form,
            "answer": answer,
            "error_message": error_message,
            "qa_available": public_status.configured,
            "api_revision": public_status.api_revision,
        },
    )


def _question_from_handoff(
    request: HttpRequest,
    *,
    token: str,
    course_id: str,
    class_id: str,
) -> str:
    web_runtime = runtime.get_web_runtime()
    paper_id, item_instance_id = verify_qa_handoff(
        token,
        course_id=course_id,
        class_id=class_id,
        learner_id=request.user.actor_id,
        max_age_seconds=web_runtime.container.settings.web.session_timeout_seconds,
    )
    response = web_runtime.container.coordinator.get_student_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    task = response.get("task_plan")
    paper = response.get("assessment_paper")
    if type(task) is not TaskPlan or type(paper) is not AssessmentPaper:
        raise DomainError(
            code="QA_HANDOFF_INVALID",
            module="m0",
            message="题目答疑入口无效或已过期。",
            recoverable=True,
        )
    detail = next(
        (
            candidate
            for candidate in question_feedback_details(task=task, paper=paper)
            if candidate.item_instance_id == item_instance_id
        ),
        None,
    )
    if detail is None:
        raise DomainError(
            code="QA_HANDOFF_INVALID",
            module="m0",
            message="题目答疑入口无效或已过期。",
            recoverable=True,
        )
    return detail.qa_prompt


def load_active_concept_context(
    *,
    course_id: str,
    class_id: str,
    release_id: str,
    concept_id: str,
) -> StudentQAContext:
    """Load verbatim source blocks and at most two active related examples."""

    references = list(
        ReleaseConceptSource.objects.filter(
            concept__release_id=release_id,
            concept__release__course_id=course_id,
            concept__release__class_id=class_id,
            concept__release__status="active",
            concept__concept_id=concept_id,
            source_version__status=CourseSourceVersion.Status.ACTIVE,
            source_version__source__course_id=course_id,
            source_version__source__class_id=class_id,
            source_version__source__source_type=CourseSource.SourceType.KNOWLEDGE,
            source_version__source__status=CourseSource.Status.ACTIVE,
        )
        .select_related("source_version__source")
        .order_by("source_version__source__display_name", "locator", "pk")[:4]
    )
    evidence = tuple(
        StudentQAEvidence(
            evidence_id=f"evidence-{reference.pk}",
            source_version_id=str(reference.source_version_id),
            file_name=reference.source_version.source.display_name,
            locator=reference.locator,
            text=reference.chunk_text,
        )
        for reference in references
    )
    links = list(
        ReleaseQuestionConceptLink.objects.filter(
            concept__release_id=release_id,
            concept__release__course_id=course_id,
            concept__release__class_id=class_id,
            concept__release__status="active",
            concept__concept_id=concept_id,
            status=ReleaseQuestionConceptLink.Status.USABLE,
            question__source_version__status=CourseSourceVersion.Status.ACTIVE,
            question__source_version__source__course_id=course_id,
            question__source_version__source__class_id=class_id,
            question__source_version__source__source_type=(
                CourseSource.SourceType.QUESTION
            ),
            question__source_version__source__status=CourseSource.Status.ACTIVE,
        )
        .select_related("question")
        .order_by("question__question_id", "pk")[:2]
    )
    examples = tuple(
        StudentQAExample(
            question_id=link.question.question_id,
            stem=link.question.stem,
            answer=_question_answer(link.question.payload),
            options=_question_options(link.question.payload),
        )
        for link in links
    )
    return StudentQAContext(evidence=evidence, examples=examples)


def build_student_qa_adapter(
    settings: ScopedDeepSeekSettings,
) -> DeepSeekStudentQAAdapter:
    """Build QA only from the exact teacher-owned credential."""

    client = DeepSeekClient(
        api_key=settings.api_key,
        model_name=settings.model_name,
        thinking_enabled=settings.thinking_enabled,
        temperature=0.0,
        max_attempts=3,
    )
    web_search_client = DeepSeekClient(
        api_key=settings.api_key,
        model_name="deepseek-v4-flash",
        thinking_enabled=settings.thinking_enabled,
        temperature=0.0,
        max_attempts=3,
    )
    return DeepSeekStudentQAAdapter(
        client,
        web_search_client=web_search_client,
        privacy_reviewer=_DeterministicQAInputReviewer(),
    )


class _DeterministicQAInputReviewer:
    """Allow only after M7's deterministic redaction gate has run."""

    reviewer_id = "student-qa-deterministic-v1"

    def review(self, text: str) -> PrivacyReviewResult:
        del text
        return PrivacyReviewResult(
            decision="allow",
            reason_codes=("deterministic_redaction_applied",),
            reviewer_ids=(self.reviewer_id,),
        )


def _active_release(course_id: str, class_id: str):
    workspace = CourseClassWorkspace.objects.select_related(
        "active_release"
    ).filter(
        course_id=course_id,
        class_id=class_id,
        active_release__course_id=course_id,
        active_release__class_id=class_id,
        active_release__status="active",
    ).first()
    return None if workspace is None else workspace.active_release


def _active_concepts(release) -> tuple[StudentQAConcept, ...]:
    if release is None:
        return ()
    rows = (
        ReleaseConcept.objects.filter(
            release=release,
            source_references__source_version__status=(
                CourseSourceVersion.Status.ACTIVE
            ),
            source_references__source_version__source__course_id=(
                release.course_id
            ),
            source_references__source_version__source__class_id=(
                release.class_id
            ),
            source_references__source_version__source__source_type=(
                CourseSource.SourceType.KNOWLEDGE
            ),
            source_references__source_version__source__status=(
                CourseSource.Status.ACTIVE
            ),
        )
        .distinct()
        .order_by("concept_id")
    )
    return tuple(
        StudentQAConcept(
            concept_id=row.concept_id,
            name=row.name,
            aliases=tuple(str(alias) for alias in row.aliases),
        )
        for row in rows
    )


def _question_answer(payload: object) -> str:
    if not isinstance(payload, dict):
        return "教师未提供参考答案。"
    for key in ("answers", "accepted_answers"):
        answers = payload.get(key)
        if isinstance(answers, list):
            cleaned = [
                str(answer).strip()
                for answer in answers
                if isinstance(answer, str) and answer.strip()
            ]
            if cleaned:
                return "；".join(cleaned)
    answer = payload.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    for key in ("explanation", "rubric"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "教师未提供参考答案。"


def _question_options(payload: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(payload, dict):
        return ()
    raw_options = payload.get("options")
    if not isinstance(raw_options, dict):
        return ()
    options = [
        (label.strip(), text.strip())
        for label, text in raw_options.items()
        if (
            isinstance(label, str)
            and label.strip()
            and isinstance(text, str)
            and text.strip()
        )
    ]
    return tuple(sorted(options, key=lambda option: option[0]))


def _insufficient() -> StudentQAAnswer:
    return StudentQAAnswer(
        status="insufficient_evidence",
        analysis="当前知识包中没有足够依据回答这个问题。",
    )


__all__ = [
    "StudentQAForm",
    "build_student_qa_adapter",
    "load_active_concept_context",
    "page",
]
