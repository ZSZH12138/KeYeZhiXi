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
from course_insight.contracts.tasking import TaskPlan
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
    SuggestionDecisionForm,
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
    ScopeSelectionForm,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    analytics_view,
    audit_view,
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
from course_insight.modules.m0_platform.correction_records import (
    load_records,
    records_path,
)
from course_insight.modules.m0_platform.objective_answers import (
    bundle_with_overlays,
    overlay_path,
    save_overlays,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    StatePolicy,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
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
            "class_form": ScopeSelectionForm(),
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
            {"lookup_form": form, "class_form": ScopeSelectionForm(), "knowledge_lookup_form": KnowledgeReviewLookupForm()},
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
def class_lookup(request: HttpRequest) -> HttpResponse:
    form = ScopeSelectionForm(data=request.GET)
    if not form.is_valid():
        return render(
            request,
            "course_insight/teacher/home.html",
            {
                "lookup_form": ReviewLookupForm(),
                "class_form": form,
                "knowledge_lookup_form": KnowledgeReviewLookupForm(),
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
    web_runtime = runtime.get_web_runtime()
    course = web_runtime.require_course(course_id)
    bundle = _knowledge_bundle(course)
    analytics = None
    m9 = getattr(web_runtime.container, "m9_service", None)
    getter = getattr(m9, "get_latest_analytics", None) if m9 is not None else None
    if callable(getter):
        raw = getter(course_id=course_id, class_id=class_id)
        if raw is not None:
            learner_states = _list_learner_states(
                web_runtime,
                course_id=course_id,
                class_id=class_id,
            )
            analytics = analytics_view(
                raw,
                knowledge_bundle=bundle,
                learner_states=learner_states,
                records=load_records(records_path(course.state_policy_path)),
                course_id=course_id,
            )
    suggestion_flow = None
    if analytics is not None:
        suggestion_flow = issue_flow_token(
            purpose="teacher_suggestion",
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
        )
    preview = _preview_paper(
        bundle,
        course_id=course_id,
        class_id=class_id,
        learner_id=request.user.actor_id,
    )
    return render(
        request,
        "course_insight/teacher/class.html",
        {
            "course_id": course_id,
            "class_id": class_id,
            "analytics": analytics,
            "preview": (
                None
                if preview is None
                else paper_view(
                    preview,
                    section_purposes=blueprint_section_purposes(
                        bundle,
                        preview.blueprint_id,
                    ),
                )
            ),
            "suggestion_flow": suggestion_flow,
            "suggestion_url": reverse(
                "teacher-class-suggestion",
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
    suggestion_flow = None
    if analytics is not None:
        suggestion_flow = issue_flow_token(
            purpose="teacher_suggestion",
            actor_id=request.user.actor_id,
            course_id=course_id,
            class_id=class_id,
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
            "suggestion_flow": suggestion_flow,
            "suggestion_url": reverse(
                "teacher-class-suggestion",
                kwargs={
                    "course_id": course_id,
                    "class_id": class_id,
                },
            ),
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
        knowledge_bundle=_knowledge_bundle(course),
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
            knowledge_bundle=_knowledge_bundle(course),
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
                "class_form": ScopeSelectionForm(),
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
    published = course.course_context.knowledge_bundle
    items = [item for item in published.items if item.is_objective()]
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
    merged = bundle_with_overlays(published, course.state_policy_path)
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
    published = course.course_context.knowledge_bundle
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
    overlay = stored if stored.get("sections") else default_overlay(published)
    merged = _knowledge_bundle(course)
    preview = _preview_paper(
        merged,
        course_id=course_id,
        class_id=class_id,
        learner_id=request.user.actor_id,
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
    bundle = _knowledge_bundle(course)
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
        records=load_records(records_path(course.state_policy_path)),
        mastered_threshold=mastered,
        consolidating_threshold=consolidating,
        misconception_activation_threshold=misconception,
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
            knowledge_bundle=_knowledge_bundle(course),
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
    bundle = bundle_with_overlays(
        course.course_context.knowledge_bundle,
        course.state_policy_path,
    )
    return bundle_with_blueprint(bundle, course.state_policy_path)


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
    approved = [
        blueprint
        for blueprint in bundle.blueprints
        if (
            blueprint.course_id == course_id
            and " ".join(blueprint.status.split()).casefold() == "teacher_approved"
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
