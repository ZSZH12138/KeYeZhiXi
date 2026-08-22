"""Teacher HTTP flow over exact grants and Coordinator use cases."""

from __future__ import annotations

from collections.abc import Mapping

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect, render
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
from course_insight.infrastructure.deepseek_secrets import (
    public_deepseek_status,
    save_teacher_deepseek_settings,
)
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.authz import (
    authorize_deepseek_config,
    authorize_scope,
)
from course_insight.modules.m0_platform.django_app.flow_tokens import (
    flow_issued_at,
    issue_flow_token,
    stable_flow_identifier,
    verify_flow_token,
)
from course_insight.modules.m0_platform.django_app.knowledge_review_flow import (
    issue_knowledge_review_token,
    verify_knowledge_review_token,
)
from course_insight.modules.m0_platform.django_app.forms.deepseek import (
    DeepSeekSettingsForm,
)
from course_insight.modules.m0_platform.django_app.forms.review import (
    TeacherReviewForm,
)
from course_insight.modules.m0_platform.django_app.forms.knowledge_review import (
    KnowledgeReviewForm,
)
from course_insight.modules.m0_platform.django_app.forms.knowledge_lookup import (
    KnowledgeReviewLookupForm,
)
from course_insight.modules.m0_platform.django_app.forms.start import (
    ReviewLookupForm,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    analytics_view,
    audit_view,
    criterion_caps,
    paper_view,
    teacher_review_view,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    TeacherReviewRecord,
)


@login_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm(
        "m0_platform_web.view_class_analytics"
    ):
        raise PermissionDenied
    return render(
        request,
        "course_insight/teacher/home.html",
        {
            "lookup_form": ReviewLookupForm(),
            "knowledge_lookup_form": KnowledgeReviewLookupForm(),
            "can_configure_deepseek": request.user.has_perm(
                "m0_platform_web.configure_deepseek"
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def deepseek_settings(request: HttpRequest) -> HttpResponse:
    authorize_deepseek_config(request.user)
    web_runtime = runtime.get_web_runtime()
    runtime_dir = web_runtime.container.settings.runtime_dir
    status = public_deepseek_status(runtime_dir)
    if request.method == "POST":
        form = DeepSeekSettingsForm(data=request.POST)
        if form.is_valid():
            api_key = str(form.cleaned_data.get("api_key") or "").strip()
            try:
                status = save_teacher_deepseek_settings(
                    runtime_dir,
                    api_key=api_key or None,
                    model_name=str(form.cleaned_data["model_name"]),
                    thinking_enabled=bool(
                        form.cleaned_data.get("thinking_enabled")
                    ),
                    clear_key=bool(form.cleaned_data.get("clear_stored_key")),
                )
            except ValueError:
                form.add_error("api_key", "密钥格式无效")
            else:
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
        {"form": form, "status": status},
    )


@login_required
@require_GET
def lookup(request: HttpRequest) -> HttpResponse:
    form = ReviewLookupForm(data=request.GET)
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/home.html",
            {"lookup_form": form},
            status=400,
        )
    course_id = str(form.cleaned_data["course_id"])
    class_id = str(form.cleaned_data["class_id"])
    paper_id = str(form.cleaned_data["paper_id"])
    _authorize_teacher(
        request,
        "view_student_report",
        course_id=course_id,
        class_id=class_id,
    )
    return redirect(
        "teacher-review-context",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
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
    return render(
        request,
        "course_insight/teacher/class.html",
        {
            "course_id": course_id,
            "class_id": class_id,
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
    response = _teacher_context(
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    waiting_status = response.get("waiting_status")
    analytics = None
    if "analytics" in response:
        analytics = _contract(response, "analytics", TeacherAnalyticsBundle)
    current_audits = _current_audits(scoring)
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
    review_links = tuple(
        (
            audit_view(audit),
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
    return render(
        request,
        "course_insight/teacher/context.html",
        {
            "paper": paper_view(paper),
            "analytics": None if analytics is None else analytics_view(analytics),
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
    analytics = (
        None
        if "analytics" not in response
        else _contract(response, "analytics", TeacherAnalyticsBundle)
    )
    audit = scoring.get_audit_record(audit_id)
    caps = criterion_caps(
        paper=paper,
        audit=audit,
        knowledge_bundle=course.course_context.knowledge_bundle,
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
    with bind_log_context(course_id=course_id, class_id=class_id):
        web_runtime.container.coordinator.review_assessment(
            paper_id=paper_id,
            review_submission=submission,
            request_id=stable_flow_identifier("request", flow),
            knowledge_bundle=course.course_context.knowledge_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=(
                course.teacher_threshold_policy_path
            ),
            course_id=course_id,
            class_id=class_id,
            index_ref=course.course_context.evidence_index_ref,
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
    form = KnowledgeReviewLookupForm(data=request.GET)
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/home.html",
            {
                "lookup_form": ReviewLookupForm(),
                "knowledge_lookup_form": form,
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
        "teacher-knowledge-review",
        course_id=course_id,
        class_id=class_id,
        review_id=str(form.cleaned_data["review_id"]),
    )


@login_required
@require_http_methods(["GET", "POST"])
def knowledge_review(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    review_id: str,
) -> HttpResponse:
    """Review M3 knowledge-package governance state through a signed flow."""

    _authorize_teacher(
        request,
        "view_class_analytics",
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
    review_record = _knowledge_review_record(web_runtime, review_id)
    package = getattr(course.course_context, "course_package", None)
    package_id = getattr(package, "course_package_id", None)
    if not isinstance(package_id, str) or review_record.subject_id != package_id:
        raise DomainError(
            code="M3_REVIEW_NOT_FOUND",
            module="m3",
            message="knowledge review does not belong to this course",
            recoverable=True,
        )
    if request.method == "GET":
        flow = issue_knowledge_review_token(
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
            review_id=review_id,
            review_version=review_record.version,
        )
        return _render_knowledge_review(
            request,
            review=review_record,
            flow=flow,
            form=KnowledgeReviewForm(
                initial={"action": _default_knowledge_action(review_record.state)},
                allowed_actions=_knowledge_actions(review_record.state),
            ),
        )

    flow = _single_flow_value(request.POST)
    verify_knowledge_review_token(
        flow,
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        review_id=review_id,
        review_version=review_record.version,
        max_age_seconds=web_runtime.container.settings.web.session_timeout_seconds,
    )
    form = KnowledgeReviewForm(
        data=_without_flow(request.POST),
        allowed_actions=_knowledge_actions(review_record.state),
    )
    if not form.is_valid():
        return _render_knowledge_review(
            request,
            review=review_record,
            flow=flow,
            form=form,
            status=400,
        )
    action = str(form.cleaned_data["action"])
    reason = str(form.cleaned_data["reason"])
    method = getattr(web_runtime.container.coordinator, f"{action}_knowledge_review", None)
    if not callable(method):
        raise DomainError(
            code="M3_REVIEW_ACTION_INVALID",
            module="m3",
            message="knowledge review action is unavailable",
            recoverable=True,
        )
    method(
        review_id,
        request.user.actor_id,
        reason,
        review_record.version,
        timezone.now(),
    )
    return redirect(
        "teacher-knowledge-review",
        course_id=course_id,
        class_id=class_id,
        review_id=review_id,
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
            index_ref=course.course_context.evidence_index_ref,
            knowledge_bundle=course.course_context.knowledge_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=course.teacher_threshold_policy_path,
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
    return (
        runtime.get_web_runtime()
        .container.coordinator.get_teacher_review_context(
            paper_id=paper_id,
            course_id=course_id,
            class_id=class_id,
        )
    )


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


def _knowledge_review_record(web_runtime: object, review_id: str) -> TeacherReviewRecord:
    try:
        value = web_runtime.container.coordinator.get_knowledge_review(
            review_id=review_id
        )
    except AttributeError as error:
        raise DomainError(
            code="M3_REVIEW_NOT_FOUND",
            module="m3",
            message="knowledge review is unavailable",
            recoverable=True,
        ) from error
    if type(value) is not TeacherReviewRecord:
        raise DomainError(
            code="WEB_RESPONSE_INVALID",
            module="m0",
            message="knowledge review response is invalid",
        )
    return value


def _default_knowledge_action(state: str) -> str:
    return {
        "draft": "submit",
        "submitted": "approve",
        "approved": "recall",
        "rejected": "recall",
        "recalled": "",
    }.get(state, "")


def _knowledge_actions(state: str) -> tuple[str, ...]:
    return {
        "draft": ("submit",),
        "submitted": ("approve", "reject"),
        "approved": ("recall",),
        "rejected": ("recall",),
        "recalled": (),
    }.get(state, ())


def _render_knowledge_review(
    request: HttpRequest,
    *,
    review: TeacherReviewRecord,
    flow: str,
    form: KnowledgeReviewForm,
    status: int = 200,
) -> HttpResponse:
    return render(
        request,
        "course_insight/teacher/knowledge_review.html",
        {
            "review": review,
            "flow": flow,
            "form": form,
            "can_change": bool(_knowledge_actions(review.state)),
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
