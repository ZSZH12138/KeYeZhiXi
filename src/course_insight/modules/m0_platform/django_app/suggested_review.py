"""Transactional projection of low-confidence profile scores to a teacher queue."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from course_insight.contracts.assessment import AssessmentPaper, ScoringResultBundle
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app.learning_projection import (
    PROFILE_TASK_TYPES,
)
from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    SuggestedTeacherReviewCase,
    SuggestedTeacherReviewItem,
    User,
)


@transaction.atomic
def sync_suggested_review_case(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    task: TaskPlan,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
) -> SuggestedTeacherReviewCase | None:
    """Create or refresh one grouped queue record; exclude non-profile tasks."""

    if task.task_type not in PROFILE_TASK_TYPES:
        return None
    if (
        task.course_id != course_id
        or task.class_id != class_id
        or task.learner_id != learner.actor_id
        or paper.learner_id != learner.actor_id
        or scoring.learner_id != learner.actor_id
        or paper.paper_id != scoring.paper_id
        or paper.task_id != task.task_id
    ):
        raise ValueError("suggested review scope does not match assessment contracts")

    workspace, _ = CourseClassWorkspace.objects.get_or_create(
        course_id=course_id,
        class_id=class_id,
    )
    current = _current_audits(scoring)
    pending = {
        audit.item_instance_id: audit
        for audit in current
        if audit.needs_review()
    }
    case = SuggestedTeacherReviewCase.objects.select_for_update().filter(
        workspace=workspace,
        attempt_id=scoring.attempt_id,
        paper_id=paper.paper_id,
    ).first()
    if not pending:
        if case is not None:
            case.status = SuggestedTeacherReviewCase.Status.RESOLVED
            case.scoring_checksum = scoring.content_checksum()
            case.save(update_fields=("status", "scoring_checksum", "updated_at"))
            case.items.filter(status=SuggestedTeacherReviewItem.Status.OPEN).update(
                status=SuggestedTeacherReviewItem.Status.RESOLVED
            )
        return case

    if case is None:
        case = SuggestedTeacherReviewCase.objects.create(
            workspace=workspace,
            learner=learner,
            attempt_id=scoring.attempt_id,
            paper_id=paper.paper_id,
            task_type=task.task_type,
            scoring_checksum=scoring.content_checksum(),
            attempted_at=task.created_at,
            status=SuggestedTeacherReviewCase.Status.OPEN,
        )
    else:
        case.learner = learner
        case.task_type = task.task_type
        case.scoring_checksum = scoring.content_checksum()
        case.status = SuggestedTeacherReviewCase.Status.OPEN
        case.save(
            update_fields=(
                "learner",
                "task_type",
                "scoring_checksum",
                "status",
                "updated_at",
            )
        )

    open_instance_ids = set(pending)
    case.items.filter(status=SuggestedTeacherReviewItem.Status.OPEN).exclude(
        item_instance_id__in=open_instance_ids
    ).update(status=SuggestedTeacherReviewItem.Status.RESOLVED)
    for audit in pending.values():
        case.items.filter(
            item_instance_id=audit.item_instance_id,
            status=SuggestedTeacherReviewItem.Status.OPEN,
        ).exclude(audit_version=audit.audit_version).update(
            status=SuggestedTeacherReviewItem.Status.SUPERSEDED
        )
        SuggestedTeacherReviewItem.objects.update_or_create(
            case=case,
            item_instance_id=audit.item_instance_id,
            audit_version=audit.audit_version,
            defaults={
                "audit_id": audit.audit_id,
                "confidence": Decimal(str(audit.confidence)),
                "review_reasons": list(audit.review_reason),
                "status": SuggestedTeacherReviewItem.Status.OPEN,
            },
        )
    return case


def _current_audits(scoring: ScoringResultBundle):
    latest = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.item_instance_id)
        if current is None or audit.audit_version > current.audit_version:
            latest[audit.item_instance_id] = audit
    return tuple(latest.values())


__all__ = ["sync_suggested_review_case"]
