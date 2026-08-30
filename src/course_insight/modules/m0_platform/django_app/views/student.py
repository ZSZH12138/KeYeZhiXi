"""Student HTTP flow: authorize, form-to-contract, Coordinator, PRG."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import replace

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
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
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.blueprint_overlay import (
    bundle_with_blueprint,
)
from course_insight.modules.m0_platform.follow_up import (
    build_follow_up_bundle,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    StatePolicy,
)
from course_insight.infrastructure.log_context import bind_log_context
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.authz import (
    authorize_scope,
    resolve_account_type,
)
from course_insight.modules.m0_platform.django_app.assessment_feedback import (
    question_feedback_details,
    teacher_notes_for_attempt,
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
from course_insight.modules.m0_platform.django_app.learning_projection import (
    PROFILE_TASK_TYPES,
    link_correction_follow_up,
    project_finalized_assessment,
    record_finalized_correction,
    selection_context_for_learner,
)
from course_insight.modules.m0_platform.django_app.suggested_review import (
    sync_suggested_review_case,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    LearnerConceptMastery,
    WrongQuestionRecord,
)
from course_insight.modules.m0_platform.django_app.qa_handoff import (
    issue_qa_handoff,
)
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
    StudentWorkspaceSelectionForm,
)
from course_insight.modules.m0_platform.django_app.workspace_labels import (
    ScopeDisplay,
    scope_display,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    blueprint_section_purposes,
    correction_guide_view,
    count_mastery_profile_view,
    feedback_view,
    item_correction_notes,
    paper_view,
    student_profile_view,
    student_result_view,
)
from course_insight.modules.m0_platform.objective_answers import (
    bundle_with_overlays,
)
from course_insight.modules.m3_knowledge_bundle.release_compatibility import (
    knowledge_bundle_from_release,
)


logger = logging.getLogger(__name__)


@login_required
@require_GET
def home(request: HttpRequest) -> HttpResponse:
    _authorize_student_entry(request)
    return render(
        request,
        "course_insight/student/home.html",
        _student_home_context(request),
    )


@login_required
@require_GET
def select_workspace(request: HttpRequest) -> HttpResponse:
    """Resolve one invited workspace selected by its human-readable label."""

    _authorize_student_entry(request)
    workspaces = _active_student_workspaces(request)
    form = StudentWorkspaceSelectionForm(
        data=request.GET,
        workspace_choices=_workspace_choices(workspaces),
    )
    if not form.is_valid():
        return render(
            request,
            "course_insight/student/home.html",
            _student_home_context(request, workspace_form=form),
            status=400,
        )
    selected_id = str(form.cleaned_data["workspace_id"])
    workspace = next(
        (
            item
            for item in workspaces
            if str(item.workspace_id) == selected_id
        ),
        None,
    )
    if workspace is None:
        return render(
            request,
            "course_insight/student/home.html",
            _student_home_context(request, workspace_form=form),
            status=400,
        )
    _authorize_student(
        request,
        "start_assessment",
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    )
    return redirect(
        "student-start",
        course_id=workspace.course_id,
        class_id=workspace.class_id,
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
            _start_context(
                request,
                web_runtime,
                course,
                class_id=class_id,
                form=AssessmentStartForm(initial={"flow_token": token}),
            ),
        )

    form = AssessmentStartForm(data=request.POST)
    if not form.is_valid():
        return render(
            request,
            "course_insight/student/start.html",
            _start_context(
                request,
                web_runtime,
                course,
                class_id=class_id,
                form=form,
            ),
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
    try:
        try:
            bundle = _active_release_bundle(course_id, class_id)
            selection_context = selection_context_for_learner(
                course_id=course_id,
                class_id=class_id,
                learner=request.user,
                current_item_ids=tuple(
                    item.item_id for item in bundle.approved_items()
                ),
            )
        except DomainError:
            if not _legacy_test_runtime():
                raise
            bundle = _knowledge_bundle(course)
            selection_context = None
        with bind_log_context(course_id=course_id, class_id=class_id):
            result = web_runtime.container.coordinator.start_assessment(
                student_text=_assessment_intent_text(
                    str(form.cleaned_data["task_type_hint"])
                ),
                task_type_hint=str(form.cleaned_data["task_type_hint"]),
                course_id=course_id,
                class_id=class_id,
                learner_id=request.user.actor_id,
                session_id=stable_flow_identifier("session", token),
                knowledge_bundle=bundle,
                selection_context=selection_context,
            )
    except DomainError as error:
        form.add_error(None, error.message)
        return render(
            request,
            "course_insight/student/start.html",
            _start_context(
                request,
                web_runtime,
                course,
                class_id=class_id,
                form=form,
            ),
            status=400,
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
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    coordinator = web_runtime.container.coordinator
    result = coordinator.get_pending_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    paper = _contract(result, "assessment_paper", AssessmentPaper)
    task_value = result.get("task_plan")
    bundle = (
        _bundle_for_task(
            task_value,
            course_id=course_id,
            class_id=class_id,
            legacy_course=course,
        )
        if type(task_value) is TaskPlan
        else _legacy_test_bundle(course)
    )
    form = AssessmentSubmissionForm(
        paper=paper,
        learner_id=request.user.actor_id,
    )
    return render(
        request,
        "course_insight/student/assessment.html",
        {
            "paper": paper_view(
                paper,
                section_purposes=blueprint_section_purposes(
                    bundle,
                    paper.blueprint_id,
                ),
            ),
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
            "source_correction_url": _source_correction_url(
                request,
                course,
                course_id=course_id,
                class_id=class_id,
                paper_id=paper_id,
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
    task_value = pending.get("task_plan")
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
    knowledge_bundle = (
        _bundle_for_task(
            task_value,
            course_id=course_id,
            class_id=class_id,
            legacy_course=course,
        )
        if type(task_value) is TaskPlan
        else _legacy_test_bundle(course)
    )
    index_ref = evidence_index_for_assessment(
        web_runtime,
        course,
        task=task_value,
        knowledge_bundle=knowledge_bundle,
        course_id=course_id,
        class_id=class_id,
    )
    class_roster_snapshot = capture_profile_class_roster(
        course_id=course_id,
        class_id=class_id,
        learner_id=request.user.actor_id,
    )
    with bind_log_context(
        course_id=course_id,
        class_id=class_id,
        attempt_id=attempt_id,
    ):
        submitted = web_runtime.container.coordinator.submit_assessment(
            assessment_submission=submission,
            request_id=stable_flow_identifier("request", flow),
            index_ref=index_ref,
            knowledge_bundle=knowledge_bundle,
            state_policy_path=course.state_policy_path,
            teacher_threshold_policy_path=(
                course.teacher_threshold_policy_path
            ),
            class_roster_snapshot=class_roster_snapshot,
        )
    _project_response_if_final(
        submitted,
        request=request,
        course_id=course_id,
        class_id=class_id,
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
    web_runtime = runtime.get_web_runtime()
    response = web_runtime.container.coordinator.get_student_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    if "waiting_status" in response and "scoring_result" not in response:
        return render(
            request,
            "course_insight/student/waiting.html",
            {
                "waiting_status": response["waiting_status"],
                "paper": paper_view(paper),
            },
        )
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    task_value = response.get("task_plan")
    if "waiting_status" in response:
        return render(
            request,
            "course_insight/student/pending_result.html",
            {
                "waiting_status": response["waiting_status"],
                "paper": paper_view(paper),
                "question_details": _question_details_with_qa(
                    task=task_value,
                    paper=paper,
                    scoring=scoring,
                    request=request,
                    course_id=course_id,
                    class_id=class_id,
                    paper_id=paper_id,
                ),
            },
        )
    if type(task_value) is TaskPlan:
        _project_response_if_final(
            response,
            request=request,
            course_id=course_id,
            class_id=class_id,
        )
    elif not _legacy_test_runtime():
        raise DomainError(
            code="WEB_RESPONSE_INVALID",
            module="m0",
            message="application response is invalid",
        )
    feedback = _contract(response, "feedback", StudentFeedbackPackage)
    course = web_runtime.require_course(course_id)
    result = student_result_view(paper, scoring, feedback)
    question_details = (
        ()
        if result.score_pending_rescore
        else _question_details_with_qa(
            task=task_value,
            paper=paper,
            scoring=scoring,
            request=request,
            course_id=course_id,
            class_id=class_id,
            paper_id=paper_id,
        )
    )
    profile_result = (
        type(task_value) is TaskPlan
        and task_value.task_type in PROFILE_TASK_TYPES
    )
    correction_url = None
    if result.correction_available and profile_result:
        correction_url = _url_with_flow(
            "student-correction",
            flow,
            course_id=course_id,
            class_id=class_id,
            paper_id=paper_id,
        )
    feedback_url = (
        _url_with_flow(
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
        )
        if profile_result
        else None
    )
    rendered = render(
        request,
        "course_insight/student/result.html",
        {
            "result": result,
            "question_details": question_details,
            "qa_url": reverse(
                "student-qa",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
            "feedback_url": feedback_url,
            "correction_url": correction_url,
            "source_correction_url": _source_correction_url(
                request,
                course,
                course_id=course_id,
                class_id=class_id,
                paper_id=paper_id,
            ),
        },
    )
    if type(task_value) is TaskPlan and not profile_result:
        purge = getattr(
            web_runtime.container.coordinator,
            "purge_transient_assessment",
            None,
        )
        if callable(purge):
            try:
                purge(paper_id=paper.paper_id, attempt_id=scoring.attempt_id)
            except (DomainError, OSError, RuntimeError, TypeError, ValueError):
                logger.exception(
                    "transient assessment cleanup failed",
                    extra={"paper_id": paper.paper_id},
                )
    return rendered


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
    package = response.get("feedback")
    if package is None:
        return render(
            request,
            "course_insight/student/waiting.html",
            {
                "waiting_status": response.get(
                    "waiting_status",
                    "awaiting_review",
                ),
                "paper": paper_view(
                    _contract(response, "assessment_paper", AssessmentPaper)
                ),
            },
        )
    package = _contract(response, "feedback", StudentFeedbackPackage)
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    task_value = response.get("task_plan")
    if type(task_value) is not TaskPlan and not _legacy_test_runtime():
        raise DomainError(
            code="WEB_RESPONSE_INVALID",
            module="m0",
            message="application response is invalid",
        )
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    web_runtime = runtime.get_web_runtime()
    web_runtime.require_course(course_id)
    records = _correction_records_for_paper(
        course_id=course_id,
        class_id=class_id,
        learner=request.user,
        paper=paper,
    )
    notes = item_correction_notes(paper, scoring, records)
    question_details = _question_details_with_qa(
        task=task_value,
        paper=paper,
        scoring=scoring,
        request=request,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    return render(
        request,
        "course_insight/student/feedback.html",
        {
            "feedback": feedback_view(
                package,
                correction_note=_correction_note(notes),
                item_notes=notes,
            ),
            "question_details": question_details,
            "qa_url": reverse(
                "student-qa",
                kwargs={"course_id": course_id, "class_id": class_id},
            ),
        },
    )


@login_required
@require_GET
def correction(
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
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    response = web_runtime.container.coordinator.get_student_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    if "scoring_result" not in response:
        return render(
            request,
            "course_insight/student/waiting.html",
            {
                "waiting_status": response.get(
                    "waiting_status",
                    "awaiting_review",
                ),
                "paper": paper_view(paper),
            },
        )
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    hint_requested = str(request.GET.get("hint", "")).strip() == "1"
    task_value = response.get("task_plan")
    bundle = (
        _bundle_for_task(
            task_value,
            course_id=course_id,
            class_id=class_id,
            legacy_course=course,
        )
        if type(task_value) is TaskPlan
        else _legacy_test_bundle(course)
    )
    guide = correction_guide_view(
        paper,
        scoring,
        bundle,
        hint_revealed=False,
    )
    records = _correction_records_for_paper(
        course_id=course_id,
        class_id=class_id,
        learner=request.user,
        paper=paper,
    )
    if hint_requested:
        _mark_correction_hints(
            course_id=course_id,
            class_id=class_id,
            learner=request.user,
            item_ids=tuple(
                paper.get_item_instance(item.item_instance_id).item_id
                for item in guide.lost_items
            ),
        )
        records = _correction_records_for_paper(
            course_id=course_id,
            class_id=class_id,
            learner=request.user,
            paper=paper,
        )
    hint_revealed = _has_revealed_hint(records, paper_id=paper_id)
    guide = correction_guide_view(
        paper,
        scoring,
        bundle,
        hint_revealed=hint_revealed,
        records=records,
    )
    hint_url = _url_with_flow(
        "student-correction",
        flow,
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    ) + "&hint=1"
    return render(
        request,
        "course_insight/student/correction.html",
        {
            "guide": guide,
            "hint_url": hint_url,
            "flow": flow,
            "follow_up_url": reverse(
                "student-follow-up",
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
def follow_up(
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> HttpResponse:
    _authorize_student(
        request,
        "start_assessment",
        course_id=course_id,
        class_id=class_id,
    )
    flow = _single_flow_value(request.POST)
    _verify(
        flow,
        request=request,
        purpose="student_result",
        course_id=course_id,
        class_id=class_id,
        paper_id=paper_id,
    )
    instance_id = str(request.POST.get("item_instance_id") or "").strip()
    if not instance_id:
        raise DomainError(
            code="FOLLOW_UP_ITEM_REQUIRED",
            module="m0",
            message="a lost item is required for follow-up practice",
            recoverable=True,
        )
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    response = web_runtime.container.coordinator.get_student_assessment(
        paper_id=paper_id,
        learner_id=request.user.actor_id,
    )
    paper = _contract(response, "assessment_paper", AssessmentPaper)
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    task_value = response.get("task_plan")
    bundle = (
        _bundle_for_task(
            task_value,
            course_id=course_id,
            class_id=class_id,
            legacy_course=course,
        )
        if type(task_value) is TaskPlan
        else _legacy_test_bundle(course)
    )
    guide = correction_guide_view(paper, scoring, bundle)
    if instance_id not in {item.item_instance_id for item in guide.lost_items}:
        raise DomainError(
            code="FOLLOW_UP_ITEM_INVALID",
            module="m0",
            message="follow-up practice is only available for a lost item",
            recoverable=True,
        )
    instance = paper.get_item_instance(instance_id)
    source_item = bundle.get_item(instance.item_id, instance.item_version)
    follow_bundle, follow_item = build_follow_up_bundle(
        bundle,
        source_item=source_item,
        salt=f"{paper_id}:{instance_id}",
    )
    start_token = issue_flow_token(
        purpose="assessment_start",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
    )
    with bind_log_context(course_id=course_id, class_id=class_id):
        started = web_runtime.container.coordinator.start_assessment(
            student_text="请根据本次失分点进行订正跟练",
            task_type_hint="correction",
            course_id=course_id,
            class_id=class_id,
            learner_id=request.user.actor_id,
            session_id=stable_flow_identifier("session", start_token),
            knowledge_bundle=follow_bundle,
        )
    follow_paper = _contract(started, "assessment_paper", AssessmentPaper)
    link_correction_follow_up(
        course_id=course_id,
        class_id=class_id,
        learner=request.user,
        source_paper_id=paper_id,
        source_item_instance_id=instance_id,
        source_item_id=source_item.item_id,
        follow_up_paper_id=follow_paper.paper_id,
        follow_up_item_id=follow_item.item_id,
    )
    submit_flow = issue_flow_token(
        purpose="assessment_submit",
        actor_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        paper_id=follow_paper.paper_id,
    )
    return _redirect_with_flow(
        "student-assessment",
        submit_flow,
        course_id=course_id,
        class_id=class_id,
        paper_id=follow_paper.paper_id,
    )


def _correction_note(notes) -> str:
    if not notes:
        return "尚未订正"
    statuses = {note.status for note in notes}
    if statuses <= {"尚未订正"}:
        return "尚未订正"
    if "尚未订正" in statuses:
        return "部分订正"
    if "提示后改对" in statuses:
        return "已订正（使用了提示）"
    return "已订正"


def _assessment_intent_text(task_type: str) -> str:
    return {
        "diagnostic": "请开始诊断测评",
        "practice": "请开始随心练习",
        "correction": "请开始订正",
        "stage_assessment": "请开始阶段评测",
    }[task_type]


def _start_context(request, web_runtime, course, *, class_id: str, form):
    history = load_profile_assessment_history(
        course_id=course.course_id,
        class_id=class_id,
        learner=request.user,
        coordinator=web_runtime.container.coordinator,
    )
    return {
        "form": form,
        "profile": _student_profile(
            web_runtime,
            course,
            class_id=class_id,
            learner_id=request.user.actor_id,
        ),
        "assessment_history": tuple(
            replace(
                item,
                result_url=_url_with_flow(
                    "student-result",
                    issue_flow_token(
                        purpose="student_result",
                        actor_id=request.user.actor_id,
                        course_id=course.course_id,
                        class_id=class_id,
                        paper_id=item.paper_id,
                    ),
                    course_id=course.course_id,
                    class_id=class_id,
                    paper_id=item.paper_id,
                ),
            )
            if item.result_available
            else item
            for item in history
        ),
    }


def _question_details_with_qa(
    *,
    task: object,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
    request: HttpRequest,
    course_id: str,
    class_id: str,
    paper_id: str,
):
    if type(task) is not TaskPlan:
        return ()
    web_runtime = runtime.get_web_runtime()
    return tuple(
        replace(
            detail,
            qa_token=issue_qa_handoff(
                course_id=course_id,
                class_id=class_id,
                learner_id=request.user.actor_id,
                paper_id=paper_id,
                item_instance_id=detail.item_instance_id,
            ),
        )
        for detail in question_feedback_details(
            task=task,
            paper=paper,
            scoring=scoring,
            answers=_frozen_answers(web_runtime, scoring.attempt_id),
            teacher_notes=teacher_notes_for_attempt(scoring.attempt_id),
        )
    )


def _frozen_answers(web_runtime, attempt_id: str) -> dict[str, object]:
    """Read the server-owned frozen answer payload without exposing failures."""

    loader = getattr(
        web_runtime.container.coordinator,
        "frozen_assessment_submission",
        None,
    )
    if not callable(loader):
        return {}
    try:
        submission = loader(attempt_id)
    except (DomainError, TypeError, ValueError):
        return {}
    answers = getattr(submission, "answers", None)
    return dict(answers) if isinstance(answers, Mapping) else {}


def _student_profile(web_runtime, course, *, class_id: str, learner_id: str):
    try:
        bundle = _active_release_bundle(course.course_id, class_id)
    except DomainError:
        if _legacy_test_runtime():
            bundle = _knowledge_bundle(course)
        else:
            return None
    records = LearnerConceptMastery.objects.filter(
        workspace__course_id=course.course_id,
        workspace__class_id=class_id,
        learner__actor_id=learner_id,
    ).order_by("concept_id")
    return count_mastery_profile_view(
        mastery_records=tuple(records),
        knowledge_bundle=bundle,
    )


def _legacy_student_profile(web_runtime, course, *, class_id: str, learner_id: str):
    snapshot = _latest_snapshot(
        web_runtime,
        course_id=course.course_id,
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
    return student_profile_view(
        snapshot=snapshot,
        knowledge_bundle=_knowledge_bundle(course),
        mastered_threshold=mastered,
        consolidating_threshold=consolidating,
        misconception_activation_threshold=misconception,
        progress=_learner_progress(
            web_runtime,
            course_id=course.course_id,
            class_id=class_id,
            learner_id=learner_id,
        ),
    )


def _course_bundle(course):
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


def _knowledge_bundle(course):
    return _course_bundle(course)


def _legacy_test_runtime() -> bool:
    """Keep pre-release Web test doubles isolated from production behavior."""

    return os.environ.get("COURSE_INSIGHT_ALLOW_LEGACY_TEST_BUNDLE") == "1"


def _legacy_test_bundle(course):
    if not _legacy_test_runtime():
        raise DomainError(
            code="WEB_RESPONSE_INVALID",
            module="m0",
            message="application response is invalid",
        )
    return _knowledge_bundle(course)


def _active_release_bundle(course_id: str, class_id: str):
    workspace = CourseClassWorkspace.objects.select_related("active_release").filter(
        course_id=course_id,
        class_id=class_id,
        active_release__course_id=course_id,
        active_release__class_id=class_id,
        active_release__status=CourseKnowledgeRelease.Status.ACTIVE,
    ).first()
    if workspace is None or workspace.active_release is None:
        raise DomainError(
            code="TEACHER_QUESTION_BANK_EMPTY",
            module="m0",
            message="课程尚未发布可用题目：教师尚未上传并发布可用题目，试卷生成失败。",
            recoverable=True,
        )
    bundle = knowledge_bundle_from_release(workspace.active_release)
    if not bundle.approved_items() or not bundle.blueprints:
        raise DomainError(
            code="TEACHER_QUESTION_BANK_EMPTY",
            module="m0",
            message="课程尚未发布可用题目：教师尚未上传并发布可用题目，试卷生成失败。",
            recoverable=True,
        )
    return bundle


def _bundle_for_task(
    task: TaskPlan,
    *,
    course_id: str,
    class_id: str,
    legacy_course=None,
):
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
        if _legacy_test_runtime() and legacy_course is not None:
            return _knowledge_bundle(legacy_course)
        raise DomainError(
            code="FROZEN_KNOWLEDGE_RELEASE_MISSING",
            module="m0",
            message="该试卷绑定的课程版本已不可用。",
        )
    return knowledge_bundle_from_release(release)


def _project_response_if_final(
    response: Mapping[str, object],
    *,
    request: HttpRequest,
    course_id: str,
    class_id: str,
) -> None:
    task = response.get("task_plan")
    paper = response.get("assessment_paper")
    scoring = response.get("scoring_result")
    if not (
        type(task) is TaskPlan
        and type(paper) is AssessmentPaper
        and type(scoring) is ScoringResultBundle
    ):
        return
    if task.task_type in PROFILE_TASK_TYPES:
        sync_suggested_review_case(
            course_id=course_id,
            class_id=class_id,
            learner=request.user,
            task=task,
            paper=paper,
            scoring=scoring,
        )
        project_finalized_assessment(
            course_id=course_id,
            class_id=class_id,
            learner=request.user,
            task=task,
            paper=paper,
            scoring=scoring,
        )
    elif task.task_type == "correction":
        record_finalized_correction(
            course_id=course_id,
            class_id=class_id,
            learner=request.user,
            task=task,
            paper=paper,
            scoring=scoring,
        )


def _source_correction_url(
    request: HttpRequest,
    course,
    *,
    course_id: str,
    class_id: str,
    paper_id: str,
) -> str | None:
    del request, course, course_id, class_id, paper_id
    return None


def _correction_records_for_paper(
    *,
    course_id: str,
    class_id: str,
    learner,
    paper: AssessmentPaper,
) -> dict[str, object]:
    rows = WrongQuestionRecord.objects.filter(
        workspace__course_id=course_id,
        workspace__class_id=class_id,
        learner=learner,
        item_id__in=tuple(item.item_id for item in paper.all_items()),
    )
    by_item_id = {row.item_id: row for row in rows}
    items: dict[str, object] = {}
    for instance in paper.all_items():
        row = by_item_id.get(instance.item_id)
        if row is None:
            continue
        items[instance.item_instance_id] = {
            "follow_up_correct": row.status == WrongQuestionRecord.Status.RESOLVED,
            "hint_revealed": row.hint_revealed,
        }
    return {paper.paper_id: {"items": items}}


def _mark_correction_hints(
    *,
    course_id: str,
    class_id: str,
    learner,
    item_ids: tuple[str, ...],
) -> None:
    if not item_ids:
        return
    WrongQuestionRecord.objects.filter(
        workspace__course_id=course_id,
        workspace__class_id=class_id,
        learner=learner,
        item_id__in=item_ids,
        status=WrongQuestionRecord.Status.OPEN,
    ).update(hint_revealed=True)


def _has_revealed_hint(records, *, paper_id: str) -> bool:
    payload = records.get(paper_id, {})
    items = payload.get("items", {}) if isinstance(payload, dict) else {}
    return any(
        isinstance(item, dict) and item.get("hint_revealed")
        for item in items.values()
    )


def _latest_snapshot(web_runtime, *, course_id: str, class_id: str, learner_id: str):
    m5 = getattr(web_runtime.container, "m5_service", None)
    getter = getattr(m5, "get_latest_learner_state", None) if m5 is not None else None
    if not callable(getter):
        return None
    return getter(course_id, class_id, learner_id)


def _learner_progress(web_runtime, *, course_id: str, class_id: str, learner_id: str):
    m5 = getattr(web_runtime.container, "m5_service", None)
    latest_getter = (
        getattr(m5, "get_latest_learner_state", None) if m5 is not None else None
    )
    exact_getter = (
        getattr(m5, "get_learner_state_exact", None) if m5 is not None else None
    )
    if not callable(latest_getter):
        return ()
    latest = latest_getter(course_id, class_id, learner_id)
    if latest is None:
        return ()
    points: list[tuple[int, float]] = []
    if callable(exact_getter):
        for version in range(1, latest.state_version + 1):
            snapshot = exact_getter(course_id, class_id, learner_id, version)
            if snapshot is not None:
                points.append((snapshot.state_version, snapshot.overall_mastery))
    if not points:
        points.append((latest.state_version, latest.overall_mastery))
    return tuple(points)


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


def _authorize_student_entry(request: HttpRequest) -> None:
    """Authorize the student landing page before a scope has been selected."""

    if (
        resolve_account_type(request.user) != AccountType.STUDENT
        or not request.user.has_perm("m0_platform_web.start_assessment")
    ):
        raise PermissionDenied


def _active_student_workspaces(
    request: HttpRequest,
) -> tuple[CourseClassWorkspace, ...]:
    """Return only the requester's currently active class memberships."""

    return tuple(
        CourseClassWorkspace.objects.filter(
            status=CourseClassWorkspace.Status.ACTIVE,
            memberships__student=request.user,
            memberships__status=ClassMembership.Status.ACTIVE,
            memberships__removed_at__isnull=True,
        )
        .distinct()
        .order_by(
            "course_display_name",
            "class_display_name",
            "course_id",
            "class_id",
        )
    )


def _workspace_choices(
    workspaces: tuple[CourseClassWorkspace, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            str(workspace.workspace_id),
            scope_display(
                course_id=workspace.course_id,
                class_id=workspace.class_id,
                course_display_name=workspace.course_display_name,
                class_display_name=workspace.class_display_name,
            ).label,
        )
        for workspace in workspaces
    )


def _student_home_context(
    request: HttpRequest,
    *,
    workspace_form: StudentWorkspaceSelectionForm | None = None,
) -> dict[str, object]:
    """Render names to students while retaining opaque IDs server-side only."""

    workspaces = _active_student_workspaces(request)
    displays: tuple[ScopeDisplay, ...] = tuple(
        scope_display(
            course_id=workspace.course_id,
            class_id=workspace.class_id,
            course_display_name=workspace.course_display_name,
            class_display_name=workspace.class_display_name,
        )
        for workspace in workspaces
    )
    return {
        "workspace_form": (
            workspace_form
            if workspace_form is not None
            else StudentWorkspaceSelectionForm(
                workspace_choices=_workspace_choices(workspaces)
            )
        ),
        "available_workspaces": displays,
    }


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
