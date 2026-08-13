"""Student HTTP flow: authorize, form-to-contract, Coordinator, PRG."""

from __future__ import annotations

from collections.abc import Mapping

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.tutoring import StudentFeedbackPackage
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.authz import authorize_scope
from course_insight.modules.m0_platform.django_app.flow_tokens import (
    flow_issued_at,
    issue_flow_token,
    stable_flow_identifier,
    verify_flow_token,
)
from course_insight.modules.m0_platform.django_app.forms.assessment import (
    AssessmentSubmissionForm,
)
from course_insight.modules.m0_platform.django_app.forms.start import (
    AssessmentStartForm,
    ScopeSelectionForm,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    feedback_view,
    paper_view,
    student_result_view,
)


@login_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    if not request.user.has_perm(
        "m0_platform_web.start_assessment"
    ):
        raise PermissionDenied
    return render(
        request,
        "course_insight/student/home.html",
        {"scope_form": ScopeSelectionForm()},
    )


@login_required
@require_GET
def select_scope(request: HttpRequest) -> HttpResponse:
    form = ScopeSelectionForm(data=request.GET)
    if not form.is_valid():
        return render(
            request,
            "course_insight/student/home.html",
            {"scope_form": form},
            status=400,
        )
    course_id = str(form.cleaned_data["course_id"])
    class_id = str(form.cleaned_data["class_id"])
    _authorize_student(
        request,
        "start_assessment",
        course_id=course_id,
        class_id=class_id,
    )
    return redirect(
        "student-start",
        course_id=course_id,
        class_id=class_id,
    )


@login_required
@require_http_methods(["GET", "POST"])
def start(
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> HttpResponse:
    _authorize_student(
        request,
        "start_assessment",
        course_id=course_id,
        class_id=class_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    if request.method == "GET":
        token = issue_flow_token(
            purpose="assessment_start",
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
        )
        return render(
            request,
            "course_insight/student/start.html",
            {"form": AssessmentStartForm(initial={"flow_token": token})},
        )

    form = AssessmentStartForm(data=request.POST)
    if not form.is_valid():
        return render(
            request,
            "course_insight/student/start.html",
            {"form": form},
            status=400,
        )
    token = str(form.cleaned_data["flow_token"])
    _verify(
        token,
        request=request,
        purpose="assessment_start",
        course_id=course_id,
        class_id=class_id,
    )
    with bind_log_context(course_id=course_id, class_id=class_id):
        result = web_runtime.container.coordinator.start_assessment(
            student_text=str(form.cleaned_data["student_text"]),
            task_type_hint=str(form.cleaned_data["task_type_hint"]),
            course_id=course_id,
            class_id=class_id,
            learner_id=request.user.actor_id,
            session_id=stable_flow_identifier("session", token),
            knowledge_bundle=course.course_context.knowledge_bundle,
        )
    paper = _contract(result, "assessment_paper", AssessmentPaper)
    flow = issue_flow_token(
        purpose="assessment_submit",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper.paper_id,
    )
    return _redirect_with_flow(
        "student-assessment",
        flow,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper.paper_id,
    )


@login_required
@require_GET
def assessment(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_student(
        request,
        "submit_assessment",
        course_id=course_id,
        class_id=class_id,
    )
    flow = request.GET.get("flow", "")
    _verify(
        flow,
        request=request,
        purpose="assessment_submit",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    coordinator = runtime.get_web_runtime().container.coordinator
    result = coordinator.get_pending_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    paper = _contract(result, "assessment_paper", AssessmentPaper)
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=request.user.actor_id,
    )
    return render(
        request,
        "course_insight/student/assessment.html",
        {
            "paper": paper_view(paper),
            "form": form,
            "flow": flow,
            "submit_url": reverse(
                "student-submit",
                kwargs={
                    "course_id": course_id,
                    "class_id": class_id,
                    "paper_id": paper_id,
                },
            ),
        },
    )


@login_required
@require_POST
def submit(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_student(
        request,
        "submit_assessment",
        course_id=course_id,
        class_id=class_id,
    )
    flow = _single_flow_value(request.POST)
    verified = _verify(
        flow,
        request=request,
        purpose="assessment_submit",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    try:
        pending = web_runtime.container.coordinator.get_pending_assessment(
            paper_id=paper_id,
            learner_id=request.user.actor_id,
        )
    except DomainError as error:
        if error.code == "ASSESSMENT_ALREADY_SUBMITTED":
            return _result_redirect(
                request,
                course_id=course_id,
                class_id=class_id,
                paper_id=paper_id,
            )
        raise
    paper = _contract(pending, "assessment_paper", AssessmentPaper)
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=request.user.actor_id,
        data=_without_flow(request.POST),
    )
    if not form.is_valid():
        return render(
            request,
            "course_insight/student/assessment.html",
            {
                "paper": paper_view(paper),
                "form": form,
                "flow": flow,
                "submit_url": request.path,
            },
            status=400,
        )
    attempt_id = stable_flow_identifier("attempt", flow)
    submission = form.to_submission(
        submission_id=stable_flow_identifier("submission", flow),
        attempt_id=attempt_id,
        submitted_at=flow_issued_at(verified),
    )
    with bind_log_context(
        course_id=course_id,
        class_id=class_id,
        attempt_id=attempt_id,
    ):
        web_runtime.container.coordinator.submit_assessment(
            assessment_submission=submission,
            request_id=stable_flow_identifier("request", flow),
            index_ref=course.course_context.evidence_index_ref,
            knowledge_bundle=course.course_context.knowledge_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=(
                course.teacher_threshold_policy_path
            ),
        )
    return _result_redirect(
        request,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )


@login_required
@require_GET
def result(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_student(
        request,
        "view_own_result",
        course_id=course_id,
        class_id=class_id,
    )
    flow = request.GET.get("flow", "")
    _verify(
        flow,
        request=request,
        purpose="student_result",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    response = runtime.get_web_runtime().container.coordinator.get_student_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    feedback = _contract(response, "feedback", StudentFeedbackPackage)
    return render(
        request,
        "course_insight/student/result.html",
        {
            "result": student_result_view(paper, scoring, feedback),
            "feedback_url": _url_with_flow(
                "student-feedback",
                issue_flow_token(
                    purpose="student_feedback",
                    actor_id=request.user.actor_id,
                    course_id=course_id,
                    class_id=class_id,
                    paper_id=paper_id,
                ),
                course_id=course_id,
                class_id=class_id,
                paper_id=paper_id,
            ),
        },
    )


@login_required
@require_GET
def feedback(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_student(
        request,
        "view_own_feedback",
        course_id=course_id,
        class_id=class_id,
    )
    flow = request.GET.get("flow", "")
    _verify(
        flow,
        request=request,
        purpose="student_feedback",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    response = runtime.get_web_runtime().container.coordinator.get_student_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    package = _contract(response, "feedback", StudentFeedbackPackage)
    return render(
        request,
        "course_insight/student/feedback.html",
        {"feedback": feedback_view(package)},
    )


def _authorize_student(
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
        learner_id=request.user.actor_id,
    )


def _verify(
    token: str,
    *,
    request: HttpRequest,
    purpose: str,
    course_id: str,
    class_id: str,
    paper_id: str | None = None,
) -> dict[str, object]:
    return verify_flow_token(
        token,
        purpose=purpose,  # type: ignore[arg-type]
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
        max_age_seconds=(
            runtime.get_web_runtime().container.settings.web
            .session_timeout_seconds
        ),
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


def _result_redirect(
    request: HttpRequest,
    *,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    flow = issue_flow_token(
        purpose="student_result",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    return _redirect_with_flow(
        "student-result",
        flow,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )


def _url_with_flow(
    name: str,
    flow: str,
    **kwargs: str,
) -> str:
    return f"{reverse(name, kwargs=kwargs)}?flow={flow}"


def _redirect_with_flow(
    name: str,
    flow: str,
    **kwargs: str,
) -> HttpResponse:
    return redirect(_url_with_flow(name, flow, **kwargs))
