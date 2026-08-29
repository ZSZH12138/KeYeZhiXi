"""Teacher HTTP flow over exact grants and Coordinator use cases."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from course_insight.contracts.analytics import TeacherAnalyticsBundle
from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.infrastructure.deepseek_secrets import (
    public_deepseek_status,
    save_teacher_deepseek_settings,
)
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.class_management import (
    add_student,
    create_owned_workspace,
    remove_student,
)
from course_insight.modules.m0_platform.django_app.authz import (
    authorize_course_knowledge,
    authorize_deepseek_config,
    authorize_scope,
)
from course_insight.modules.m0_platform.django_app.flow_tokens import (
    flow_issued_at,
    issue_flow_token,
    stable_flow_identifier,
    verify_flow_token,
)
from course_insight.modules.m0_platform.django_app.forms.deepseek import (
    DeepSeekSettingsForm,
)
from course_insight.modules.m0_platform.django_app.forms.governance import (
    ClassMemberForm,
    OpenClassForm,
)
from course_insight.modules.m0_platform.django_app.learning_projection import (
    PROFILE_TASK_TYPES,
    correction_records_view,
    project_finalized_assessment,
)
from course_insight.modules.m0_platform.django_app.suggested_review import (
    sync_suggested_review_case,
)
from course_insight.modules.m0_platform.django_app.assessment_history import (
    load_profile_assessment_history,
)
from course_insight.modules.m0_platform.django_app.assessment_evidence import (
    evidence_index_for_assessment,
)
from course_insight.modules.m0_platform.django_app.class_roster import (
    capture_profile_class_roster,
)
from course_insight.modules.m0_platform.django_app.assessment_feedback import (
    QuestionFeedbackDetail,
    question_feedback_details,
    teacher_notes_for_attempt,
)
from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    ClassMembership,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    LearnerConceptMastery,
    RoleName,
    SuggestedTeacherReviewCase,
    SuggestedTeacherReviewItem,
    TeacherItemReviewNote,
    User,
)
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    ScopedDeepSeekSettings,
    resolve_scoped_deepseek_settings,
    save_scoped_deepseek_settings,
    scoped_deepseek_status,
)
from course_insight.modules.m0_platform.django_app.teaching_advice import (
    build_class_teaching_advice_prompt,
)
from course_insight.modules.m0_platform.django_app.forms.review import (
    SuggestionDecisionForm,
    TeacherItemRescoreForm,
    TeacherReviewForm,
)
from course_insight.modules.m0_platform.django_app.forms.start import (
    ReviewLookupForm,
    ScopeSelectionForm,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    analytics_view,
    blueprint_section_purposes,
    criterion_caps,
    learner_review_view,
    paper_view,
    teacher_review_view,
)
from course_insight.modules.m0_platform.blueprint_overlay import (
    bundle_with_blueprint,
    default_overlay,
    load_overlay,
    overlay_path as blueprint_overlay_path,
    save_overlay,
)
from course_insight.modules.m0_platform.objective_answers import (
    bundle_with_overlays,
    overlay_path,
    save_overlays,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    StatePolicy,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    ensure_current_class_learning_snapshot,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from course_insight.modules.m9_teacher_analytics.teaching_advice import (
    TeachingAdviceResult,
    generate_teaching_advice,
)
from course_insight.modules.m3_knowledge_bundle.release_compatibility import (
    knowledge_bundle_from_release,
)


@dataclass(frozen=True, slots=True)
class TeacherQuestionReviewView:
    """One question, current score, and exactly one bounded rescore action."""

    detail: QuestionFeedbackDetail
    form: TeacherItemRescoreForm | None
    action_url: str | None


@login_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm(
        "m0_platform_web.view_class_analytics"
    ):
        raise PermissionDenied
    grant_scopes = {
        (course_id, class_id)
        for course_id, class_id in request.user.actor_grants.filter(
            is_active=True,
            revoked_at__isnull=True,
            class_id__isnull=False,
        ).values_list("course_id", "class_id")
        if course_id and class_id
    }
    owned_workspaces = tuple(
        CourseClassWorkspace.objects.filter(
            owner_teacher=request.user,
            status=CourseClassWorkspace.Status.ACTIVE,
        ).order_by("course_display_name", "class_display_name", "course_id")
    )
    knowledge_scopes = sorted(
        {
            *grant_scopes,
            *((item.course_id, item.class_id) for item in owned_workspaces),
        }
    )
    return render(
        request,
        "course_insight/teacher/home.html",
        {
            "lookup_form": ReviewLookupForm(),
            "class_form": ScopeSelectionForm(),
            "open_class_form": OpenClassForm(
                initial={"request_token": secrets.token_urlsafe(32)}
            ),
            "owned_workspaces": owned_workspaces,
            "can_configure_deepseek": request.user.has_perm(
                "m0_platform_web.configure_deepseek"
            ),
            "knowledge_scopes": tuple(
                {"course_id": course_id, "class_id": class_id}
                for course_id, class_id in knowledge_scopes
            ),
            "knowledge_courses": sorted(
                {course_id for course_id, _ in knowledge_scopes}
            ),
            "can_manage_course_knowledge": request.user.has_perm(
                "m0_platform_web.manage_course_knowledge"
            ),
        },
    )


@login_required
@require_POST
def open_class(request: HttpRequest) -> HttpResponse:
    form = OpenClassForm(data=request.POST)
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/home.html",
            {
                "lookup_form": ReviewLookupForm(),
                "class_form": ScopeSelectionForm(),
                "open_class_form": form,
                "owned_workspaces": CourseClassWorkspace.objects.none(),
                "knowledge_scopes": (),
                "knowledge_courses": (),
                "can_configure_deepseek": request.user.has_perm(
                    "m0_platform_web.configure_deepseek"
                ),
                "can_manage_course_knowledge": request.user.has_perm(
                    "m0_platform_web.manage_course_knowledge"
                ),
            },
            status=400,
        )
    workspace = create_owned_workspace(
        teacher=request.user,
        course_name=str(form.cleaned_data["course_name"]),
        class_name=str(form.cleaned_data["class_name"]),
        request_token=str(form.cleaned_data["request_token"]),
    )
    return redirect(
        "teacher-class",
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    )


@login_required
@require_POST
def add_class_student(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    workspace = get_object_or_404(
        CourseClassWorkspace,
        course_id=course_id,
        class_id=class_id,
    )
    form = ClassMemberForm(data=request.POST)
    if not form.is_valid():
        return HttpResponse("学生账户名无效", status=400)
    try:
        add_student(
            workspace=workspace,
            teacher=request.user,
            actor_id=str(form.cleaned_data["student_account"]),
        )
    except ValidationError as error:
        return HttpResponse(str(error.message), status=400)
    return redirect("teacher-class", course_id=course_id, class_id=class_id)


@login_required
@require_POST
def remove_class_student(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    actor_id: str,
) -> HttpResponse:
    workspace = get_object_or_404(
        CourseClassWorkspace,
        course_id=course_id,
        class_id=class_id,
    )
    try:
        remove_student(
            workspace=workspace,
            teacher=request.user,
            actor_id=actor_id,
        )
    except ValidationError as error:
        return HttpResponse(str(error.message), status=400)
    return redirect("teacher-class", course_id=course_id, class_id=class_id)


@login_required
@require_http_methods(["GET", "POST"])
def deepseek_settings(
    request: HttpRequest,
    course_id: str | None = None,
    class_id: str | None = None,
) -> HttpResponse:
    scoped = course_id is not None and class_id is not None
    if scoped:
        authorize_scope(
            request.user,
            "configure_deepseek",
            course_id=course_id,
            class_id=class_id,
        )
        status = scoped_deepseek_status(course_id, class_id)
        runtime_dir = None
    else:
        authorize_deepseek_config(request.user)
        web_runtime = runtime.get_web_runtime()
        runtime_dir = web_runtime.container.settings.runtime_dir
        status = public_deepseek_status(runtime_dir)
    if request.method == "POST":
        form = DeepSeekSettingsForm(data=request.POST)
        if form.is_valid():
            api_key = str(form.cleaned_data.get("api_key") or "").strip()
            try:
                if scoped:
                    status = save_scoped_deepseek_settings(
                        course_id=course_id,
                        class_id=class_id,
                        api_key=api_key or None,
                        model_name=str(form.cleaned_data["model_name"]),
                        thinking_enabled=bool(
                            form.cleaned_data.get("thinking_enabled")
                        ),
                        clear_key=bool(
                            form.cleaned_data.get("clear_stored_key")
                        ),
                        updated_by=request.user,
                    )
                else:
                    status = save_teacher_deepseek_settings(
                        runtime_dir,
                        api_key=api_key or None,
                        model_name=str(form.cleaned_data["model_name"]),
                        thinking_enabled=bool(
                            form.cleaned_data.get("thinking_enabled")
                        ),
                        clear_key=bool(
                            form.cleaned_data.get("clear_stored_key")
                        ),
                    )
            except ValueError:
                form.add_error("api_key", "密钥格式无效")
            else:
                if scoped:
                    return redirect(
                        "teacher-deepseek-settings",
                        course_id=course_id,
                        class_id=class_id,
                    )
                runtime.close_application_container()
                return redirect("teacher-deepseek-settings")
    else:
        form = DeepSeekSettingsForm(
            initial={
                "model_name": status.model_name,
                "thinking_enabled": status.thinking_enabled,
            }
        )
    return render(
        request,
        "course_insight/teacher/deepseek.html",
        {
            "form": form,
            "status": status,
            "course_id": course_id,
            "class_id": class_id,
            "scoped": scoped,
        },
    )


@login_required
@require_GET
def lookup(request: HttpRequest) -> HttpResponse:
    form = ReviewLookupForm(data=request.GET)
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/home.html",
            {"lookup_form": form, "class_form": ScopeSelectionForm()},
            status=400,
        )
    course_id = str(form.cleaned_data["course_id"])
    class_id = str(form.cleaned_data["class_id"])
    learner_id = str(form.cleaned_data["learner_account"])
    _authorize_teacher(
        request,
        "view_student_report",
        course_id=course_id,
        class_id=class_id,
    )
    return redirect(
        "teacher-learner-review-list",
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
    )


@login_required
@require_GET
def review_list(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    learner_id: str,
) -> HttpResponse:
    """List every finalized assessment that currently affects this profile."""

    _authorize_teacher(
        request,
        "view_student_report",
        course_id=course_id,
        class_id=class_id,
    )
    _authorize_teacher(
        request,
        "review_score",
        course_id=course_id,
        class_id=class_id,
    )
    learner = _require_active_student(
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
    )
    coordinator = runtime.get_web_runtime().container.coordinator
    history = load_profile_assessment_history(
        course_id=course_id,
        class_id=class_id,
        learner=learner,
        coordinator=coordinator,
    )
    review_entries = tuple(
        replace(
            item,
            result_url=reverse(
                "teacher-review-context",
                kwargs={
                    "course_id": course_id,
                    "class_id": class_id,
                    "paper_id": item.paper_id,
                },
            ),
        )
        for item in history
    )
    return render(
        request,
        "course_insight/teacher/review_list.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "learner": learner,
            "assessment_history": review_entries,
        },
    )


@login_required
@require_GET
def class_lookup(request: HttpRequest) -> HttpResponse:
    form = ScopeSelectionForm(data=request.GET)
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/home.html",
            {
                "lookup_form": ReviewLookupForm(),
                "class_form": form,
            },
            status=400,
        )
    course_id = str(form.cleaned_data["course_id"])
    class_id = str(form.cleaned_data["class_id"])
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    return redirect(
        "teacher-class",
        course_id=course_id,
        class_id=class_id,
    )


@login_required
@require_GET
def class_context(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    return _render_class_context(
        request,
        course_id=course_id,
        class_id=class_id,
    )


@login_required
@require_POST
def generate_class_teaching_advice(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    """Generate one teacher-requested advice text from the current M5 view."""

    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    flow = _single_flow_value(request.POST)
    verify_flow_token(
        flow,
        purpose="teacher_teaching_advice",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        max_age_seconds=(
            web_runtime.container.settings.web.session_timeout_seconds
        ),
    )
    settings = resolve_scoped_deepseek_settings(course_id, class_id)
    if settings is None:
        return _render_class_context(
            request,
            course_id=course_id,
            class_id=class_id,
            teaching_advice_error=(
                "教师尚未为当前课程班级配置 DeepSeek API，暂不能生成教学建议。"
            ),
        )
    class_snapshot = ensure_current_class_learning_snapshot(
        course_id=course_id,
        class_id=class_id,
    )
    if class_snapshot is None:
        return _render_class_context(
            request,
            course_id=course_id,
            class_id=class_id,
            teaching_advice_error=(
                "当前没有可同步的课程知识版本，暂不能生成教学建议。"
            ),
        )
    try:
        prompt = build_class_teaching_advice_prompt(
            snapshot=class_snapshot,
            weak_mastery_threshold=_class_weak_mastery_threshold(course),
        )
        teaching_advice = generate_teaching_advice(
            client=_teaching_advice_client(settings),
            prompt=prompt,
            model_ref=LLMModelRef(
                model_name=settings.model_name,
                model_version="runtime-api",
                status="configured",
            ),
            created_at=timezone.now(),
        )
    except ValueError:
        return _render_class_context(
            request,
            course_id=course_id,
            class_id=class_id,
            teaching_advice_error=(
                "当前没有已做且掌握度低于巩固线的知识点，暂不生成建议。"
            ),
        )
    except DomainError:
        return _render_class_context(
            request,
            course_id=course_id,
            class_id=class_id,
            teaching_advice_error=(
                "教学建议服务暂时不可用，请稍后重试；课程材料和学习画像不会受到影响。"
            ),
        )
    return _render_class_context(
        request,
        course_id=course_id,
        class_id=class_id,
        teaching_advice=teaching_advice,
    )


def _render_class_context(
    request: HttpRequest,
    *,
    course_id: str,
    class_id: str,
    teaching_advice: TeachingAdviceResult | None = None,
    teaching_advice_error: str | None = None,
) -> HttpResponse:
    """Render the class dashboard from the latest M5 snapshot and M9 view."""

    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    bundle = _scoped_knowledge_bundle(
        course,
        course_id=course_id,
        class_id=class_id,
    )
    # M5 rebuilds only when profile counters, the active roster, or the active
    # release changed.  M9's teacher presentation consumes this named view.
    class_snapshot = ensure_current_class_learning_snapshot(
        course_id=course_id,
        class_id=class_id,
    )
    analytics = None
    m9 = getattr(web_runtime.container, "m9_service", None)
    getter = getattr(m9, "get_latest_analytics", None) if m9 is not None else None
    if callable(getter):
        raw = getter(course_id=course_id, class_id=class_id)
        if raw is not None and bundle is not None:
            learner_states = _list_learner_states(
                web_runtime,
                course_id=course_id,
                class_id=class_id,
            )
            analytics = analytics_view(
                raw,
                knowledge_bundle=bundle,
                learner_states=learner_states,
                records=correction_records_view(
                    course_id=course_id,
                    class_id=class_id,
                ),
                course_id=course_id,
            )
    advice_status = scoped_deepseek_status(course_id, class_id)
    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
    ).first()
    can_manage_roster = (
        workspace is not None and workspace.is_owned_by(request.user)
    )
    memberships = (
        ClassMembership.objects.filter(
            workspace=workspace,
            status=ClassMembership.Status.ACTIVE,
            removed_at__isnull=True,
        )
        .select_related("student")
        .order_by("student__actor_id")
        if can_manage_roster
        else ClassMembership.objects.none()
    )
    teaching_advice_flow = None
    if class_snapshot is not None and advice_status.configured:
        teaching_advice_flow = issue_flow_token(
            purpose="teacher_teaching_advice",
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
        )
    return render(
        request,
        "course_insight/teacher/class.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "workspace": workspace,
            "can_manage_roster": can_manage_roster,
            "active_memberships": memberships,
            "active_student_count": memberships.count(),
            "member_form": ClassMemberForm(),
            "analytics": analytics,
            "class_snapshot": class_snapshot,
            "teaching_advice": teaching_advice,
            "teaching_advice_error": teaching_advice_error,
            "teaching_advice_available": advice_status.configured,
            "teaching_advice_flow": teaching_advice_flow,
            "teaching_advice_url": reverse(
                "teacher-class-teaching-advice",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
            "deepseek_settings_url": reverse(
                "teacher-deepseek-settings",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
            "objective_answers_url": reverse(
                "teacher-objective-answers",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
            "blueprint_url": reverse(
                "teacher-blueprint",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
            "suggested_review_count": SuggestedTeacherReviewCase.objects.filter(
                workspace__course_id=course_id,
                workspace__class_id=class_id,
                status=SuggestedTeacherReviewCase.Status.OPEN,
            ).count(),
            "suggested_review_url": reverse(
                "teacher-suggested-review-list",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
            "lookup_form": ReviewLookupForm(
                initial={
                    "course_id": course_id,
                    "class_id": class_id,
                }
            ),
        },
    )


@login_required
@require_GET
def review_context(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    _authorize_teacher(
        request,
        "view_student_report",
        course_id=course_id,
        class_id=class_id,
    )
    _authorize_teacher(
        request,
        "review_score",
        course_id=course_id,
        class_id=class_id,
    )
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    task_value = response.get("task_plan")
    waiting_status = response.get("waiting_status")
    analytics = None
    if "analytics" in response:
        analytics = _contract(response, "analytics", TeacherAnalyticsBundle)
    class_snapshot = ensure_current_class_learning_snapshot(
        course_id=course_id,
        class_id=class_id,
    )
    current_audits = _current_audits(scoring)
    # Keep the non-rendered legacy link collection for older internal
    # integrations.  The template deliberately exposes only one item-level
    # rejudge action, as required by the current teacher workflow.
    review_links = tuple(
        (
            audit,
            reverse(
                "teacher-review",
                kwargs={
                    "course_id": course_id,
                    "class_id": class_id,
                    "paper_id": paper_id,
                    "audit_id": audit.audit_id,
                },
            ),
        )
        for audit in current_audits
    )
    rejected = next(
        (audit for audit in current_audits if audit.is_rejected()),
        None,
    )
    rescore_flow = None
    if waiting_status == "awaiting_rescore" and rejected is not None:
        rescore_flow = issue_flow_token(
            purpose="teacher_rescore",
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
            paper_id=paper_id,
            audit_id=rejected.audit_id,
            audit_version=rejected.audit_version,
        )
    question_reviews = _question_review_views(
        course=course,
        task=task_value,
        paper=paper,
        scoring=scoring,
        coordinator=web_runtime.container.coordinator,
        reviewer_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    return render(
        request,
        "course_insight/teacher/context.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "paper": paper_view(paper),
            "analytics": None if analytics is None else analytics_view(analytics),
            "class_snapshot": class_snapshot,
            "question_reviews": question_reviews,
            "review_links": review_links,
            "waiting_status": waiting_status,
            "rescore_flow": rescore_flow,
            "rescore_url": reverse(
                "teacher-rescore",
                kwargs={
                    "course_id": course_id,
                    "class_id": class_id,
                    "paper_id": paper_id,
                },
            ),
        },
    )


@login_required
@require_GET
def suggested_review_list(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    """Show one grouped queue row per profile-affecting attempt and paper."""

    for permission in (
        "view_class_analytics",
        "view_student_report",
        "review_score",
    ):
        _authorize_teacher(
            request,
            permission,
            course_id=course_id,
            class_id=class_id,
        )
    cases = tuple(
        SuggestedTeacherReviewCase.objects.filter(
            workspace__course_id=course_id,
            workspace__class_id=class_id,
            status=SuggestedTeacherReviewCase.Status.OPEN,
        )
        .select_related("learner")
        .prefetch_related("items")
        .order_by("attempted_at", "pk")
    )
    rows = tuple(
        {
            "case": case,
            "question_ids": tuple(
                item.item_instance_id
                for item in case.items.all()
                if item.status == SuggestedTeacherReviewItem.Status.OPEN
            ),
            "detail_url": reverse(
                "teacher-suggested-review-detail",
                kwargs={
                    "course_id": course_id,
                    "class_id": class_id,
                    "case_id": case.pk,
                },
            ),
        }
        for case in cases
    )
    return render(
        request,
        "course_insight/teacher/suggested_review_list.html",
        {"course_id": course_id, "class_id": class_id, "rows": rows},
    )


@login_required
@require_GET
def suggested_review_detail(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    case_id: int,
) -> HttpResponse:
    """Show only the low-confidence questions grouped in one queue case."""

    for permission in (
        "view_class_analytics",
        "view_student_report",
        "review_score",
    ):
        _authorize_teacher(
            request,
            permission,
            course_id=course_id,
            class_id=class_id,
        )
    case = (
        SuggestedTeacherReviewCase.objects.filter(
            pk=case_id,
            workspace__course_id=course_id,
            workspace__class_id=class_id,
            status__in=(
                SuggestedTeacherReviewCase.Status.OPEN,
                SuggestedTeacherReviewCase.Status.RESOLVED,
            ),
        )
        .select_related("learner")
        .first()
    )
    if case is None:
        raise Http404("suggested review case is unavailable")
    web_runtime = runtime.get_web_runtime()
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=case.paper_id,
    )
    task = response.get("task_plan")
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    if scoring.attempt_id != case.attempt_id or paper.learner_id != case.learner.actor_id:
        raise Http404("suggested review case no longer matches the assessment")
    all_rows = _question_review_views(
        course=web_runtime.require_course(course_id),
        task=task,
        paper=paper,
        scoring=scoring,
        coordinator=web_runtime.container.coordinator,
        reviewer_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=case.paper_id,
    )
    open_ids = set(
        case.items.filter(
            status=SuggestedTeacherReviewItem.Status.OPEN
        ).values_list("item_instance_id", flat=True)
    )
    question_reviews = tuple(
        replace(
            row,
            action_url=(
                f"{row.action_url}?suggested_case_id={case.pk}"
                if row.action_url is not None
                else None
            ),
        )
        for row in all_rows
        if row.detail.item_instance_id in open_ids
    )
    return render(
        request,
        "course_insight/teacher/suggested_review_detail.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "case": case,
            "question_reviews": question_reviews,
            "review_complete": not open_ids,
        },
    )


@login_required
@require_POST
def item_rescore(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
    item_instance_id: str,
) -> HttpResponse:
    """Apply a teacher's single bounded score for one item and resync state."""

    for permission in (
        "view_class_analytics",
        "view_student_report",
        "review_score",
    ):
        _authorize_teacher(
            request,
            permission,
            course_id=course_id,
            class_id=class_id,
        )
    suggested_case = _suggested_case_for_rescore(
        request,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        item_instance_id=item_instance_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    audit = _audit_for_item(scoring, item_instance_id)
    if audit is None:
        raise Http404("assessment item is not available for review")
    flow = _single_flow_value(request.POST)
    verified = verify_flow_token(
        flow,
        purpose="teacher_item_rescore",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        audit_id=audit.audit_id,
        audit_version=audit.audit_version,
        max_age_seconds=(
            web_runtime.container.settings.web.session_timeout_seconds
        ),
    )
    task_value = response.get("task_plan")
    assessment_bundle = _assessment_bundle(
        course,
        task_value,
        course_id=course_id,
        class_id=class_id,
    )
    index_ref = evidence_index_for_assessment(
        web_runtime,
        course,
        task=task_value,
        knowledge_bundle=assessment_bundle,
        course_id=course_id,
        class_id=class_id,
    )
    form = TeacherItemRescoreForm(
        audit=audit,
        reviewer_id=request.user.actor_id,
        criterion_caps=criterion_caps(
            paper=paper,
            audit=audit,
            knowledge_bundle=assessment_bundle,
        ),
        data=request.POST,
    )
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/review.html",
            {
                "review": teacher_review_view(
                    paper,
                    audit,
                    (
                        None
                        if "analytics" not in response
                        else _contract(
                            response,
                            "analytics",
                            TeacherAnalyticsBundle,
                        )
                    ),
                ),
                "form": form,
                "flow": flow,
            },
            status=400,
        )
    submission = form.to_submission(
        submission_id=stable_flow_identifier("item-review", flow),
        submitted_at=flow_issued_at(verified),
    )
    student_evidence = _frozen_student_evidence(
        web_runtime.container.coordinator,
        scoring.attempt_id,
        item_instance_id,
    )
    class_roster_snapshot = capture_profile_class_roster(
        course_id=course_id,
        class_id=class_id,
        learner_id=paper.learner_id,
    )
    with bind_log_context(
        course_id=course_id,
        class_id=class_id,
        attempt_id=scoring.attempt_id,
    ):
        web_runtime.container.coordinator.review_assessment(
            paper_id=paper_id,
            review_submission=submission,
            request_id=stable_flow_identifier("item-review-request", flow),
            knowledge_bundle=assessment_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=course.teacher_threshold_policy_path,
            course_id=course_id,
            class_id=class_id,
            index_ref=index_ref,
            class_roster_snapshot=class_roster_snapshot,
            student_evidence=student_evidence,
        )
    _record_item_rescore_and_projection(
        web_runtime=web_runtime,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        item_instance_id=item_instance_id,
        teacher=request.user,
        teacher_note=str(form.cleaned_data.get("teacher_note") or ""),
    )
    if suggested_case is not None:
        return redirect(
            "teacher-suggested-review-detail",
            course_id=course_id,
            class_id=class_id,
            case_id=suggested_case.pk,
        )
    return redirect(
        "teacher-review-context",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )


def _suggested_case_for_rescore(
    request: HttpRequest,
    *,
    course_id: str,
    class_id: str,
    paper_id: str,
    item_instance_id: str,
) -> SuggestedTeacherReviewCase | None:
    """Resolve a bounded queue origin without accepting an arbitrary redirect."""

    values = request.GET.getlist("suggested_case_id")
    if not values:
        return None
    if len(values) != 1:
        raise Http404("suggested review origin is invalid")
    try:
        case_id = int(values[0])
    except (TypeError, ValueError):
        raise Http404("suggested review origin is invalid") from None
    if case_id < 1:
        raise Http404("suggested review origin is invalid")
    case = (
        SuggestedTeacherReviewCase.objects.filter(
            pk=case_id,
            workspace__course_id=course_id,
            workspace__class_id=class_id,
            paper_id=paper_id,
            items__item_instance_id=item_instance_id,
        )
        .distinct()
        .first()
    )
    if case is None:
        raise Http404("suggested review origin is unavailable")
    return case


@login_required
@require_http_methods(["GET", "POST"])
def review(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
    audit_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    _authorize_teacher(
        request,
        "view_student_report",
        course_id=course_id,
        class_id=class_id,
    )
    _authorize_teacher(
        request,
        "review_score",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    task_value = response.get("task_plan")
    assessment_bundle = _assessment_bundle(
        course,
        task_value,
        course_id=course_id,
        class_id=class_id,
    )
    index_ref = evidence_index_for_assessment(
        web_runtime,
        course,
        task=task_value,
        knowledge_bundle=assessment_bundle,
        course_id=course_id,
        class_id=class_id,
    )
    analytics = (
        None
        if "analytics" not in response
        else _contract(response, "analytics", TeacherAnalyticsBundle)
    )
    audit = scoring.get_audit_record(audit_id)
    caps = criterion_caps(
        paper=paper,
        audit=audit,
        knowledge_bundle=assessment_bundle,
    )
    if request.method == "GET":
        flow = issue_flow_token(
            purpose="teacher_review",
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
            paper_id=paper_id,
            audit_id=audit.audit_id,
            audit_version=audit.audit_version,
        )
        form = TeacherReviewForm(
            audit=audit,
            reviewer_id=request.user.actor_id,
            criterion_caps=caps,
            initial={
                "final_total_score": audit.total_score,
                "decision": "confirm",
            },
        )
        return _render_review(
            request,
            paper=paper,
            audit=audit,
            analytics=analytics,
            form=form,
            flow=flow,
        )

    flow = _single_flow_value(request.POST)
    verified = _verify_review(
        flow,
        request=request,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        audit=audit,
    )
    form = TeacherReviewForm(
        audit=audit,
        reviewer_id=request.user.actor_id,
        criterion_caps=caps,
        data=_without_flow(request.POST),
    )
    if not form.is_valid():
        return _render_review(
            request,
            paper=paper,
            audit=audit,
            analytics=analytics,
            form=form,
            flow=flow,
            status=400,
        )
    submission = form.to_submission(
        submission_id=stable_flow_identifier("review", flow),
        submitted_at=flow_issued_at(verified),
    )
    class_roster_snapshot = capture_profile_class_roster(
        course_id=course_id,
        class_id=class_id,
        learner_id=paper.learner_id,
    )
    with bind_log_context(course_id=course_id, class_id=class_id):
        web_runtime.container.coordinator.review_assessment(
            paper_id=paper_id,
            review_submission=submission,
            request_id=stable_flow_identifier("request", flow),
            knowledge_bundle=assessment_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=(
                course.teacher_threshold_policy_path
            ),
            course_id=course_id,
            class_id=class_id,
            index_ref=index_ref,
            class_roster_snapshot=class_roster_snapshot,
        )
    refreshed = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    refreshed_task = refreshed.get("task_plan")
    refreshed_paper = refreshed.get("assessment_paper")
    refreshed_scoring = refreshed.get("scoring_result")
    learner = User.objects.filter(actor_id=paper.learner_id).first()
    if (
        learner is not None
        and type(refreshed_task) is TaskPlan
        and type(refreshed_paper) is AssessmentPaper
        and type(refreshed_scoring) is ScoringResultBundle
    ):
        sync_suggested_review_case(
            course_id=course_id,
            class_id=class_id,
            learner=learner,
            task=refreshed_task,
            paper=refreshed_paper,
            scoring=refreshed_scoring,
        )
        project_finalized_assessment(
            course_id=course_id,
            class_id=class_id,
            learner=learner,
            task=refreshed_task,
            paper=refreshed_paper,
            scoring=refreshed_scoring,
        )
    return redirect(
        "teacher-review-context",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )


@login_required
@require_GET
def knowledge_lookup(request: HttpRequest) -> HttpResponse:
    course_id = request.GET.get("course_id", "")
    if not course_id:
        return HttpResponse("旧知识包审核入口已停用，请从课程知识文件进入。", status=410)
    authorize_course_knowledge(request.user, course_id)
    return redirect("teacher-course-knowledge", course_id=course_id)


@login_required
@require_http_methods(["GET", "POST"])
def knowledge_review(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    review_id: str,
) -> HttpResponse:
    del class_id, review_id
    authorize_course_knowledge(request.user, course_id)
    return redirect("teacher-course-knowledge", course_id=course_id)


@login_required
@require_http_methods(["GET", "POST"])
def objective_answers(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "review_score",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    published = _scoped_knowledge_bundle(
        course,
        course_id=course_id,
        class_id=class_id,
    )
    items = [] if published is None else [
        item for item in published.items if item.is_objective()
    ]
    if request.method == "POST":
        overlays: dict[str, list[str]] = {}
        for item in items:
            raw = request.POST.get(f"answers_{item.item_id}", "")
            lines = [
                line.strip()
                for line in str(raw).splitlines()
                if line.strip()
            ]
            if lines:
                overlays[item.item_id] = lines
        save_overlays(overlay_path(course.state_policy_path), overlays)
        return redirect(
            "teacher-objective-answers",
            course_id=course_id,
            class_id=class_id,
        )
    merged = (
        None
        if published is None
        else bundle_with_overlays(published, course.state_policy_path)
    )
    rows = []
    for item in items:
        current = merged.get_item(item.item_id, item.version)
        answers = current.answer_key.get("answers")
        if type(answers) is not list or not answers:
            raw_answer = current.answer_key.get("answer")
            answers = [raw_answer] if isinstance(raw_answer, str) else []
        rows.append(
            {
                "item_id": item.item_id,
                "stem": item.stem,
                "field_name": f"answers_{item.item_id}",
                "answers_text": "\n".join(str(value) for value in answers),
            }
        )
    return render(
        request,
        "course_insight/teacher/objective_answers.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "items": rows,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def blueprint(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    published = _scoped_knowledge_bundle(
        course,
        course_id=course_id,
        class_id=class_id,
    )
    path = blueprint_overlay_path(course.state_policy_path)
    if request.method == "POST":
        sections: list[dict[str, object]] = []
        for index in range(4):
            purpose = str(
                request.POST.get(f"purpose_{index}") or ""
            ).strip() or (
                "anchor",
                "uncertainty",
                "misconception",
                "remediation",
            )[index]
            anchors = [
                value.strip()
                for value in str(
                    request.POST.get(f"anchor_item_ids_{index}") or ""
                ).replace(",", "\n").splitlines()
                if value.strip()
            ]
            sections.append(
                {
                    "section_id": str(
                        request.POST.get(f"section_id_{index}")
                        or f"sec_{purpose}"
                    ).strip(),
                    "name": str(
                        request.POST.get(f"name_{index}") or ""
                    ).strip() or (
                        "公共锚点",
                        "不确定点",
                        "误区",
                        "补救",
                    )[index],
                    "purpose": purpose,
                    "item_count": _as_int(
                        request.POST.get(f"item_count_{index}"),
                        field="item_count",
                    ),
                    "score": _as_float(
                        request.POST.get(f"score_{index}"),
                        field="score",
                    ),
                    "anchor_item_ids": anchors,
                }
            )
        save_overlay(
            path,
            {
                "blueprint_id": str(
                    request.POST.get("blueprint_id") or ""
                ).strip(),
                "sections": sections,
            },
        )
        return redirect(
            "teacher-blueprint",
            course_id=course_id,
            class_id=class_id,
        )
    stored = load_overlay(path)
    overlay = (
        stored
        if stored.get("sections")
        else ({} if published is None else default_overlay(published))
    )
    merged = published
    preview = (
        None
        if merged is None
        else _preview_paper(
            merged,
            course_id=course_id,
            class_id=class_id,
            learner_id=request.user.actor_id,
        )
    )
    return render(
        request,
        "course_insight/teacher/blueprint.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "blueprint_id": overlay.get("blueprint_id") or "",
            "sections": overlay.get("sections") or [],
            "preview": (
                None
                if preview is None
                else paper_view(
                    preview,
                    section_purposes=blueprint_section_purposes(
                        merged,
                        preview.blueprint_id,
                    ),
                )
            ),
        },
    )


@login_required
@require_GET
def learner(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    learner_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    workspace = CourseClassWorkspace.objects.select_related("active_release").filter(
        course_id=course_id,
        class_id=class_id,
        active_release__status=CourseKnowledgeRelease.Status.ACTIVE,
    ).first()
    bundle = (
        knowledge_bundle_from_release(workspace.active_release)
        if workspace is not None and workspace.active_release is not None
        else _knowledge_bundle(course)
    )
    snapshot = _latest_snapshot(
        web_runtime,
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
    )
    mastered = 0.8
    consolidating = 0.4
    misconception = 0.5
    try:
        policy = StatePolicy.from_path(course.state_policy_path)
    except (DomainError, OSError, TypeError, ValueError):
        policy = None
    if policy is not None:
        mastered = policy.mastered_threshold
        consolidating = policy.consolidating_threshold
        misconception = policy.misconception_activation_threshold
    view = learner_review_view(
        learner_id=learner_id,
        knowledge_bundle=bundle,
        snapshot=snapshot,
        records=correction_records_view(
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
        ),
        mastered_threshold=mastered,
        consolidating_threshold=consolidating,
        misconception_activation_threshold=misconception,
        authoritative_mastery=tuple(
            LearnerConceptMastery.objects.filter(
                workspace__course_id=course_id,
                workspace__class_id=class_id,
                learner__actor_id=learner_id,
            ).order_by("concept_id")
        ),
    )
    return render(
        request,
        "course_insight/teacher/learner.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "review": view,
        },
    )


@login_required
@require_POST
def decide_class_suggestion(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    form = SuggestionDecisionForm(data=request.POST)
    if not form.is_valid():
        raise DomainError(
            code="SUGGESTION_DECISION_INVALID",
            module="application",
            message="teaching suggestion decision is incomplete",
            recoverable=True,
        )
    flow = str(form.cleaned_data["flow_token"])
    verify_flow_token(
        flow,
        purpose="teacher_suggestion",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=None,
        max_age_seconds=(
            runtime.get_web_runtime().container.settings.web.session_timeout_seconds
        ),
    )
    m9 = getattr(runtime.get_web_runtime().container, "m9_service", None)
    getter = getattr(m9, "get_latest_analytics", None) if m9 is not None else None
    analytics = getter(course_id=course_id, class_id=class_id) if callable(getter) else None
    if analytics is None:
        raise DomainError(
            code="ANALYTICS_UNAVAILABLE",
            module="m9",
            message="class analytics are unavailable",
            recoverable=True,
        )
    runtime.get_web_runtime().container.coordinator.apply_suggestion_decision(
        report_id=analytics.report_id,
        suggestion_id=str(form.cleaned_data["suggestion_id"]),
        decision=str(form.cleaned_data["decision"]),
        content=str(form.cleaned_data.get("content") or "") or None,
    )
    return redirect(
        "teacher-class",
        course_id=course_id,
        class_id=class_id,
    )


@login_required
@require_POST
def decide_suggestion(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "view_class_analytics",
        course_id=course_id,
        class_id=class_id,
    )
    form = SuggestionDecisionForm(data=request.POST)
    if not form.is_valid():
        raise DomainError(
            code="SUGGESTION_DECISION_INVALID",
            module="application",
            message="teaching suggestion decision is incomplete",
            recoverable=True,
        )
    flow = str(form.cleaned_data["flow_token"])
    verify_flow_token(
        flow,
        purpose="teacher_suggestion",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        max_age_seconds=(
            runtime.get_web_runtime().container.settings.web.session_timeout_seconds
        ),
    )
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    analytics = _contract(response, "analytics", TeacherAnalyticsBundle)
    runtime.get_web_runtime().container.coordinator.apply_suggestion_decision(
        report_id=analytics.report_id,
        suggestion_id=str(form.cleaned_data["suggestion_id"]),
        decision=str(form.cleaned_data["decision"]),
        content=str(form.cleaned_data.get("content") or "") or None,
    )
    return redirect(
        "teacher-review-context",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )


@login_required
@require_POST
def rescore(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_teacher(
        request,
        "review_score",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    task_value = response.get("task_plan")
    assessment_bundle = _assessment_bundle(
        course,
        task_value,
        course_id=course_id,
        class_id=class_id,
    )
    index_ref = evidence_index_for_assessment(
        web_runtime,
        course,
        task=task_value,
        knowledge_bundle=assessment_bundle,
        course_id=course_id,
        class_id=class_id,
    )
    rejected = next(
        (audit for audit in _current_audits(scoring) if audit.is_rejected()),
        None,
    )
    if rejected is None:
        raise DomainError(
            code="RESCORE_REQUEST_INVALID",
            module="application",
            message="model rescore requires a rejected audit",
            recoverable=True,
        )
    flow = _single_flow_value(request.POST)
    verify_flow_token(
        flow,
        purpose="teacher_rescore",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        audit_id=rejected.audit_id,
        audit_version=rejected.audit_version,
        max_age_seconds=(
            web_runtime.container.settings.web.session_timeout_seconds
        ),
    )
    submission = web_runtime.container.coordinator.frozen_assessment_submission(
        scoring.attempt_id
    )
    class_roster_snapshot = capture_profile_class_roster(
        course_id=course_id,
        class_id=class_id,
        learner_id=submission.learner_id,
    )
    with bind_log_context(
        course_id=course_id,
        class_id=class_id,
        attempt_id=scoring.attempt_id,
    ):
        web_runtime.container.coordinator.rescore_assessment(
            assessment_submission=submission,
            audit_id=rejected.audit_id,
            expected_rejected_version=rejected.audit_version,
            rescore_request_id=stable_flow_identifier("rescore", flow),
            request_id=stable_flow_identifier("rescore-request", flow),
            index_ref=index_ref,
            knowledge_bundle=assessment_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=course.teacher_threshold_policy_path,
            class_roster_snapshot=class_roster_snapshot,
        )
    return redirect(
        "teacher-review-context",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )


def _teacher_context(
    *,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> Mapping[str, object]:
    response = (
        runtime.get_web_runtime()
        .container.coordinator.get_teacher_review_context(
            paper_id=paper_id,
            course_id=course_id,
            class_id=class_id,
        )
    )
    task = response.get("task_plan")
    environment = getattr(
        runtime.get_web_runtime().container.settings,
        "environment",
        None,
    )
    if (
        type(task) is TaskPlan
        and task.task_type not in PROFILE_TASK_TYPES
    ) or (
        type(task) is not TaskPlan
        and environment is not None
        and environment != "test"
    ):
        raise Http404("only profile-affecting assessments can be reviewed")
    return response


def _authorize_teacher(
    request: HttpRequest,
    permission: str,
    *,
    course_id: str,
    class_id: str,
) -> None:
    authorize_scope(
        request.user,
        permission,
        course_id=course_id,
        class_id=class_id,
    )


def _require_active_student(
    *,
    course_id: str,
    class_id: str,
    learner_id: str,
) -> User:
    """Resolve an account only through the current exact student roster."""

    now = timezone.now()
    grant = (
        ActorGrant.objects.select_related("user")
        .filter(
            user__actor_id=learner_id,
            role=RoleName.STUDENT,
            course_id=course_id,
            class_id=class_id,
            is_active=True,
            revoked_at__isnull=True,
            valid_from__lte=now,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
        .first()
    )
    if grant is None:
        raise Http404("student account is not active in this class")
    return grant.user


def _verify_review(
    token: str,
    *,
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
    audit: ScoreAuditRecord,
) -> dict[str, object]:
    return verify_flow_token(
        token,
        purpose="teacher_review",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        audit_id=audit.audit_id,
        audit_version=audit.audit_version,
        max_age_seconds=(
            runtime.get_web_runtime().container.settings.web
            .session_timeout_seconds
        ),
    )


def _render_review(
    request: HttpRequest,
    *,
    paper: AssessmentPaper,
    audit: ScoreAuditRecord,
    analytics: TeacherAnalyticsBundle,
    form: TeacherReviewForm,
    flow: str,
    status: int = 200,
) -> HttpResponse:
    return render(
        request,
        "course_insight/teacher/review.html",
        {
            "review": teacher_review_view(paper, audit, analytics),
            "form": form,
            "flow": flow,
        },
        status=status,
    )


def _contract(
    values: Mapping[str, object],
    key: str,
    expected_type: type,
):
    value = values.get(key)
    if type(value) is not expected_type:
        raise DomainError(
            code="WEB_RESPONSE_INVALID",
            module="m0",
            message="application response is invalid",
        )
    return value


def _current_audits(
    scoring: ScoringResultBundle,
) -> tuple[ScoreAuditRecord, ...]:
    latest: dict[str, ScoreAuditRecord] = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.audit_id)
        if current is None or audit.audit_version > current.audit_version:
            latest[audit.audit_id] = audit
    return tuple(latest[key] for key in sorted(latest))


def _single_flow_value(data: QueryDict) -> str:
    values = data.getlist("flow_token")
    if len(values) != 1:
        raise DomainError(
            code="WEB_FLOW_TOKEN_INVALID",
            module="m0",
            message="the Web workflow token is invalid",
        )
    return values[0]


def _without_flow(data: QueryDict) -> QueryDict:
    copied = data.copy()
    copied.pop("flow_token", None)
    return copied


def _as_int(value: object, *, field: str) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError) as error:
        raise DomainError(
            code="BLUEPRINT_OVERLAY_INVALID",
            module="m0",
            message=f"{field} must be an integer",
            recoverable=True,
        ) from error


def _as_float(value: object, *, field: str) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError) as error:
        raise DomainError(
            code="BLUEPRINT_OVERLAY_INVALID",
            module="m0",
            message=f"{field} must be a number",
            recoverable=True,
        ) from error


def _knowledge_bundle(course):
    if course.course_context is None:
        raise DomainError(
            code="COURSE_KNOWLEDGE_NOT_PUBLISHED",
            module="m0",
            message="课程尚未发布可用题目。",
            recoverable=True,
        )
    bundle = bundle_with_overlays(
        course.course_context.knowledge_bundle,
        course.state_policy_path,
    )
    return bundle_with_blueprint(bundle, course.state_policy_path)


def _scoped_knowledge_bundle(
    course,
    *,
    course_id: str,
    class_id: str,
):
    workspace = CourseClassWorkspace.objects.select_related("active_release").filter(
        course_id=course_id,
        class_id=class_id,
        active_release__status=CourseKnowledgeRelease.Status.ACTIVE,
    ).first()
    if workspace is not None and workspace.active_release is not None:
        try:
            return knowledge_bundle_from_release(workspace.active_release)
        except DomainError:
            # Pre-governance static courses may contain partial release rows
            # used only by M5 snapshots. Their fixed manifest remains the
            # readable source until a complete release is published.
            if course.course_context is None:
                raise
    if course.course_context is None:
        return None
    return _knowledge_bundle(course)


def _assessment_bundle(
    course,
    task: object,
    *,
    course_id: str,
    class_id: str,
):
    """Resolve new assessments from their frozen release; keep old records readable."""

    if type(task) is not TaskPlan:
        return _knowledge_bundle(course)
    if task.course_id != course_id or task.class_id != class_id:
        raise DomainError(
            code="ASSESSMENT_SCOPE_MISMATCH",
            module="m0",
            message="试卷不属于当前课程班级。",
        )
    try:
        release = CourseKnowledgeRelease.objects.filter(
            pk=task.knowledge_bundle_id,
            course_id=course_id,
            class_id=class_id,
            status__in=(
                CourseKnowledgeRelease.Status.ACTIVE,
                CourseKnowledgeRelease.Status.RETIRED,
            ),
        ).first()
    except (ValidationError, ValueError):
        release = None
    if release is None:
        if os.environ.get("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE") == "1":
            return _knowledge_bundle(course)
        raise DomainError(
            code="FROZEN_KNOWLEDGE_RELEASE_MISSING",
            module="m0",
            message="该试卷绑定的课程版本已不可用。",
        )
    return knowledge_bundle_from_release(release)


def _latest_snapshot(web_runtime, *, course_id: str, class_id: str, learner_id: str):
    m5 = getattr(web_runtime.container, "m5_service", None)
    getter = getattr(m5, "get_latest_learner_state", None) if m5 is not None else None
    if not callable(getter):
        return None
    return getter(course_id, class_id, learner_id)


def _list_learner_states(web_runtime, *, course_id: str, class_id: str):
    m5 = getattr(web_runtime.container, "m5_service", None)
    lister = (
        getattr(m5, "list_latest_learner_states", None) if m5 is not None else None
    )
    if not callable(lister):
        return ()
    return tuple(lister(course_id, class_id))


def _preview_paper(bundle, *, course_id: str, class_id: str, learner_id: str):
    """Preview only inside the blueprint editor, never on the class dashboard."""

    approved = [
        blueprint
        for blueprint in bundle.blueprints
        if (
            blueprint.course_id == course_id
            and " ".join(blueprint.status.split()).casefold()
            == "teacher_approved"
        )
    ]
    if not approved:
        return None
    blueprint = approved[0]
    plan = TaskPlan(
        task_id="preview1",
        task_type="practice",
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
        session_id="preview1",
        blueprint_id=blueprint.blueprint_id,
        knowledge_bundle_id=bundle.knowledge_bundle_id,
        course_package_id=bundle.course_package_id,
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=timezone.now(),
    )
    try:
        return PaperGenerator().generate(plan, bundle, None, None)
    except DomainError:
        return None


def _question_review_views(
    *,
    course,
    task: object,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
    coordinator: object,
    reviewer_id: str,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> tuple[TeacherQuestionReviewView, ...]:
    """Project one teacher-rescore form for each current paper item."""

    if type(task) is not TaskPlan:
        return ()
    assessment_bundle = _assessment_bundle(
        course,
        task,
        course_id=course_id,
        class_id=class_id,
    )
    audits = {audit.item_instance_id: audit for audit in _current_audits(scoring)}
    answers = _frozen_answers_for_attempt(coordinator, scoring.attempt_id)
    details = question_feedback_details(
        task=task,
        paper=paper,
        scoring=scoring,
        answers=answers,
        teacher_notes=teacher_notes_for_attempt(scoring.attempt_id),
    )
    rows: list[TeacherQuestionReviewView] = []
    for detail in details:
        audit = audits.get(detail.item_instance_id)
        if audit is None:
            rows.append(
                TeacherQuestionReviewView(
                    detail=detail,
                    form=None,
                    action_url=None,
                )
            )
            continue
        flow = issue_flow_token(
            purpose="teacher_item_rescore",
            actor_id=reviewer_id,
            course_id=course_id,
            class_id=class_id,
            paper_id=paper_id,
            audit_id=audit.audit_id,
            audit_version=audit.audit_version,
        )
        form = TeacherItemRescoreForm(
            audit=audit,
            reviewer_id=reviewer_id,
            criterion_caps=criterion_caps(
                paper=paper,
                audit=audit,
                knowledge_bundle=assessment_bundle,
            ),
            initial={
                "score": audit.total_score,
                "teacher_note": detail.teacher_note,
                "flow_token": flow,
            },
        )
        rows.append(
            TeacherQuestionReviewView(
                detail=detail,
                form=form,
                action_url=reverse(
                    "teacher-item-rescore",
                    kwargs={
                        "course_id": course_id,
                        "class_id": class_id,
                        "paper_id": paper_id,
                        "item_instance_id": detail.item_instance_id,
                    },
                ),
            )
        )
    return tuple(rows)


def _frozen_answers_for_attempt(
    coordinator: object,
    attempt_id: str,
) -> dict[str, object]:
    loader = getattr(coordinator, "frozen_assessment_submission", None)
    if not callable(loader):
        return {}
    try:
        submission = loader(attempt_id)
    except (DomainError, TypeError, ValueError):
        return {}
    answers = getattr(submission, "answers", None)
    return dict(answers) if isinstance(answers, Mapping) else {}


def _frozen_student_evidence(
    coordinator: object,
    attempt_id: str,
    item_instance_id: str,
) -> str | None:
    """Return the server-owned submitted answer used by a positive override."""

    value = _frozen_answers_for_attempt(coordinator, attempt_id).get(
        item_instance_id
    )
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, bool):
        return "是" if value else "否"
    if type(value) in {int, float}:
        return str(value)
    return None


def _audit_for_item(
    scoring: ScoringResultBundle,
    item_instance_id: str,
) -> ScoreAuditRecord | None:
    return next(
        (
            audit
            for audit in _current_audits(scoring)
            if audit.item_instance_id == item_instance_id
        ),
        None,
    )


def _record_item_rescore_and_projection(
    *,
    web_runtime,
    course_id: str,
    class_id: str,
    paper_id: str,
    item_instance_id: str,
    teacher: User,
    teacher_note: str,
) -> None:
    """Persist the visible note and fan the finalized score into M0→M5→M9."""

    refreshed = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    task = refreshed.get("task_plan")
    paper = refreshed.get("assessment_paper")
    scoring = refreshed.get("scoring_result")
    if not (
        type(task) is TaskPlan
        and type(paper) is AssessmentPaper
        and type(scoring) is ScoringResultBundle
    ):
        return
    learner = User.objects.filter(actor_id=paper.learner_id).first()
    audit = _audit_for_item(scoring, item_instance_id)
    if learner is None or audit is None:
        return
    sync_suggested_review_case(
        course_id=course_id,
        class_id=class_id,
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    project_finalized_assessment(
        course_id=course_id,
        class_id=class_id,
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    workspace, _ = CourseClassWorkspace.objects.get_or_create(
        course_id=course_id,
        class_id=class_id,
    )
    TeacherItemReviewNote.objects.get_or_create(
        attempt_id=scoring.attempt_id,
        item_instance_id=item_instance_id,
        audit_version=audit.audit_version,
        defaults={
            "workspace": workspace,
            "learner": learner,
            "reviewed_by": teacher,
            "paper_id": paper.paper_id,
            "audit_id": audit.audit_id,
            "score": Decimal(str(audit.total_score)).quantize(
                Decimal("0.001")
            ),
            "max_score": Decimal(str(audit.max_score)).quantize(
                Decimal("0.001")
            ),
            "teacher_note": teacher_note.strip(),
        },
    )


def _class_weak_mastery_threshold(course) -> float:
    """Use the same consolidation boundary that drives student weak status."""

    return StatePolicy.from_path(course.state_policy_path).consolidating_threshold


def _teaching_advice_client(settings: ScopedDeepSeekSettings) -> DeepSeekClient:
    """Build one bounded client from the exact course/class secret only."""

    return DeepSeekClient(
        api_key=settings.api_key,
        model_name=settings.model_name,
        thinking_enabled=settings.thinking_enabled,
        temperature=0.0,
        max_tokens=900,
        max_attempts=3,
    )
