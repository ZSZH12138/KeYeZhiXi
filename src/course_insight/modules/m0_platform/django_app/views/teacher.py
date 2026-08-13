"""Teacher HTTP flow over exact grants and Coordinator use cases."""

from __future__ import annotations

from collections.abc import Mapping

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from course_insight.contracts.analytics import TeacherAnalyticsBundle
from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.authz import authorize_scope
from course_insight.modules.m0_platform.django_app.flow_tokens import (
    flow_issued_at,
    issue_flow_token,
    stable_flow_identifier,
    verify_flow_token,
)
from course_insight.modules.m0_platform.django_app.forms.review import (
    TeacherReviewForm,
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
        {"lookup_form": ReviewLookupForm()},
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
    analytics = _contract(response, "analytics", TeacherAnalyticsBundle)
    current_audits = _current_audits(scoring)
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
            "analytics": analytics_view(analytics),
            "review_links": review_links,
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
    analytics = _contract(response, "analytics", TeacherAnalyticsBundle)
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
