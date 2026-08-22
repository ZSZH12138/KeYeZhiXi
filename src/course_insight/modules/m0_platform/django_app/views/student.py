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
from course_insight.modules.m0_platform.blueprint_overlay import (
    bundle_with_blueprint,
)
from course_insight.modules.m0_platform.correction_records import (
    link_follow_up,
    load_records,
    record_follow_up_outcome,
    record_hint,
    records_path,
    save_records,
    upsert_lost_items,
)
from course_insight.modules.m0_platform.follow_up import (
    build_follow_up_bundle,
    merge_follow_up_items,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    StatePolicy,
)
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
    blueprint_section_purposes,
    correction_guide_view,
    feedback_view,
    item_correction_notes,
    paper_view,
    student_profile_view,
    student_result_view,
)
from course_insight.modules.m0_platform.objective_answers import (
    bundle_with_overlays,
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
            {
                "form": AssessmentStartForm(initial={"flow_token": token}),
                "profile": _student_profile(
                    web_runtime,
                    course,
                    class_id=class_id,
                    learner_id=request.user.actor_id,
                ),
            },
        )

    form = AssessmentStartForm(data=request.POST)
    if not form.is_valid():
        return render(
            request,
            "course_insight/student/start.html",
            {
                "form": form,
                "profile": _student_profile(
                    web_runtime,
                    course,
                    class_id=class_id,
                    learner_id=request.user.actor_id,
                ),
            },
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
            knowledge_bundle=_knowledge_bundle(course),
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
                    _knowledge_bundle(course),
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
            knowledge_bundle=_knowledge_bundle(course),
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
    feedback = _contract(response, "feedback", StudentFeedbackPackage)
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    _record_follow_up_if_needed(course, paper_id, scoring)
    result = student_result_view(paper, scoring, feedback)
    correction_url = None
    if result.correction_available:
        correction_url = _url_with_flow(
            "student-correction",
            flow,
            course_id=course_id,
            class_id=class_id,
            paper_id=paper_id,
        )
    return render(
        request,
        "course_insight/student/result.html",
        {
            "result": result,
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
            "correction_url": correction_url,
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
    scoring = _contract(response, "scoring_result", ScoringResultBundle)
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    records = _load_records(course)
    notes = item_correction_notes(paper, scoring, records)
    return render(
        request,
        "course_insight/student/feedback.html",
        {
            "feedback": feedback_view(
                package,
                correction_note=_correction_note(notes),
                item_notes=notes,
            )
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
    bundle = _knowledge_bundle(course)
    guide = correction_guide_view(
        paper,
        scoring,
        bundle,
        hint_revealed=False,
    )
    records = _persist_correction_progress(
        course,
        paper_id=paper_id,
        learner_id=request.user.actor_id,
        course_id=course_id,
        class_id=class_id,
        guide=guide,
        hint_requested=hint_requested,
    )
    hint_revealed = hint_requested or _hint_already_revealed(
        records,
        paper_id=paper_id,
        item_instance_ids=[item.item_instance_id for item in guide.lost_items],
    )
    if hint_revealed:
        guide = correction_guide_view(
            paper,
            scoring,
            bundle,
            hint_revealed=True,
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
    bundle = _knowledge_bundle(course)
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
    records = link_follow_up(
        _load_records(course),
        paper_id=paper_id,
        item_instance_id=instance_id,
        follow_up_paper_id=follow_paper.paper_id,
        follow_up_item_id=follow_item.item_id,
        source_item_id=source_item.item_id,
    )
    _save_records(course, records)
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


def _student_profile(web_runtime, course, *, class_id: str, learner_id: str):
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


def _knowledge_bundle(course):
    bundle = bundle_with_overlays(
        course.course_context.knowledge_bundle,
        course.state_policy_path,
    )
    bundle = bundle_with_blueprint(bundle, course.state_policy_path)
    return merge_follow_up_items(bundle, _load_records(course))


def _load_records(course):
    return load_records(records_path(course.state_policy_path))


def _save_records(course, records) -> None:
    save_records(records_path(course.state_policy_path), records)


def _persist_correction_progress(
    course,
    *,
    paper_id: str,
    learner_id: str,
    course_id: str,
    class_id: str,
    guide,
    hint_requested: bool,
):
    records = _load_records(course)
    if not guide.available:
        return records
    records = upsert_lost_items(
        records,
        paper_id=paper_id,
        learner_id=learner_id,
        course_id=course_id,
        class_id=class_id,
        items=tuple(
            {
                "item_instance_id": item.item_instance_id,
                "stem": item.stem,
                "cause": item.cause,
                "concept_ids": list(item.concept_ids),
            }
            for item in guide.lost_items
        ),
    )
    if hint_requested:
        for item in guide.lost_items:
            records = record_hint(records, paper_id, item.item_instance_id)
    _save_records(course, records)
    return records


def _hint_already_revealed(records, *, paper_id: str, item_instance_ids) -> bool:
    payload = records.get(paper_id, {})
    items = payload.get("items", {}) if isinstance(payload, dict) else {}
    return any(
        isinstance(items.get(item_id), dict) and items[item_id].get("hint_revealed")
        for item_id in item_instance_ids
    )


def _record_follow_up_if_needed(course, paper_id: str, scoring: ScoringResultBundle) -> None:
    if scoring.requires_teacher_review() or scoring.has_rejected_score():
        return
    records = _load_records(course)
    linked = False
    for payload in records.values():
        items = payload.get("items", {}) if isinstance(payload, dict) else {}
        if not isinstance(items, dict):
            continue
        if any(
            isinstance(item, dict) and item.get("follow_up_paper_id") == paper_id
            for item in items.values()
        ):
            linked = True
            break
    if not linked:
        return
    records = record_follow_up_outcome(
        records,
        follow_up_paper_id=paper_id,
        follow_up_correct=scoring.total_score >= scoring.max_score,
    )
    _save_records(course, records)


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
