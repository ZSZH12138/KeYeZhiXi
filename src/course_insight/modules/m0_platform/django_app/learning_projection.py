"""Transactional projection from finalized scores to simple learner records."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Sequence

from django.db import transaction

from course_insight.contracts.assessment import AssessmentPaper, ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
    CourseClassWorkspace,
    LearnerConceptMastery,
    User,
    WrongQuestionRecord,
)
from course_insight.modules.m8_assessment_scoring.selection_policy import (
    AssessmentSelectionContext,
    ConceptMastery,
)


_PROFILE_TASK_TYPES = frozenset({"diagnostic", "stage_assessment"})


@transaction.atomic
def project_finalized_assessment(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    task: TaskPlan,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
) -> bool:
    """Apply one finalized result once; return whether a projection was made."""

    _validate_projection_scope(
        course_id=course_id,
        class_id=class_id,
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    if scoring.requires_teacher_review() or scoring.has_rejected_score():
        return False
    checksum = scoring.content_checksum()
    payload = _projection_payload(paper, scoring)
    existing = AssessmentProjectionReceipt.objects.select_for_update().filter(
        attempt_id=scoring.attempt_id
    ).first()
    if existing is not None:
        if existing.scoring_checksum == checksum:
            return False
        if (
            existing.workspace.course_id != course_id
            or existing.workspace.class_id != class_id
            or existing.learner_id != learner.pk
            or existing.paper_id != paper.paper_id
            or existing.task_type != task.task_type
            or not existing.projection_payload
        ):
            raise DomainError(
                code="ASSESSMENT_PROJECTION_CONFLICT",
                module="m0",
                message="同一次作答已有不同的画像投影结果。",
            )
        _apply_revision(
            workspace=existing.workspace,
            learner=learner,
            task=task,
            attempt_id=scoring.attempt_id,
            previous=existing.projection_payload,
            current=payload,
        )
        existing.scoring_checksum = checksum
        existing.projection_payload = payload
        existing.save(update_fields=("scoring_checksum", "projection_payload"))
        return True

    workspace, _ = CourseClassWorkspace.objects.get_or_create(
        course_id=course_id,
        class_id=class_id,
    )
    for item in paper.all_items():
        correct = bool(payload[item.item_instance_id]["correct"])
        _update_wrong_ledger(
            workspace=workspace,
            learner=learner,
            item_id=item.item_id,
            item_version=item.item_version,
            attempt_id=scoring.attempt_id,
            correct=correct,
        )
        if task.task_type in _PROFILE_TASK_TYPES:
            for concept_id in item.concept_ids:
                _increment_mastery(
                    workspace=workspace,
                    learner=learner,
                    concept_id=concept_id,
                    correct=correct,
                )

    AssessmentProjectionReceipt.objects.create(
        workspace=workspace,
        learner=learner,
        attempt_id=scoring.attempt_id,
        paper_id=paper.paper_id,
        task_type=task.task_type,
        scoring_checksum=checksum,
        projection_payload=payload,
    )
    return True


def _projection_payload(
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
) -> dict[str, dict[str, object]]:
    latest_audits = _latest_audits(scoring)
    payload: dict[str, dict[str, object]] = {}
    for item in paper.all_items():
        audit = latest_audits.get(item.item_instance_id)
        if audit is None:
            raise DomainError(
                code="ASSESSMENT_PROJECTION_INCOMPLETE",
                module="m0",
                message="评分结果缺少试卷题目记录，不能更新画像。",
            )
        payload[item.item_instance_id] = {
            "item_id": item.item_id,
            "item_version": item.item_version,
            "concept_ids": list(item.concept_ids),
            "correct": math.isclose(
                audit.total_score,
                audit.max_score,
                rel_tol=0.0,
                abs_tol=1e-9,
            ),
        }
    return payload


def _apply_revision(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    task: TaskPlan,
    attempt_id: str,
    previous: object,
    current: dict[str, dict[str, object]],
) -> None:
    if not isinstance(previous, dict) or set(previous) != set(current):
        raise DomainError(
            code="ASSESSMENT_PROJECTION_CONFLICT",
            module="m0",
            message="同一次作答的试卷结构发生冲突。",
        )
    for instance_id, item in current.items():
        before = previous.get(instance_id)
        if not isinstance(before, dict) or (
            before.get("item_id") != item["item_id"]
            or before.get("concept_ids") != item["concept_ids"]
        ):
            raise DomainError(
                code="ASSESSMENT_PROJECTION_CONFLICT",
                module="m0",
                message="同一次作答的题目投影发生冲突。",
            )
        was_correct = bool(before.get("correct"))
        is_correct = bool(item["correct"])
        if was_correct == is_correct:
            continue
        if task.task_type in _PROFILE_TASK_TYPES:
            for concept_id in item["concept_ids"]:
                _revise_mastery(
                    workspace=workspace,
                    learner=learner,
                    concept_id=str(concept_id),
                    correct_delta=int(is_correct) - int(was_correct),
                )
        _update_wrong_ledger(
            workspace=workspace,
            learner=learner,
            item_id=str(item["item_id"]),
            item_version=str(item["item_version"]),
            attempt_id=attempt_id,
            correct=is_correct,
        )


@transaction.atomic
def selection_context_for_learner(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    current_item_ids: Sequence[str],
) -> AssessmentSelectionContext:
    """Read current profile and retire deleted questions from correction."""

    workspace, _ = CourseClassWorkspace.objects.get_or_create(
        course_id=course_id,
        class_id=class_id,
    )
    current_ids = frozenset(current_item_ids)
    obsolete = WrongQuestionRecord.objects.select_for_update().filter(
        workspace=workspace,
        learner=learner,
        status=WrongQuestionRecord.Status.OPEN,
    ).exclude(item_id__in=current_ids)
    obsolete.update(status=WrongQuestionRecord.Status.RETIRED)
    masteries = {
        record.concept_id: ConceptMastery(
            attempted_count=record.attempted_count,
            correct_count=record.correct_count,
        )
        for record in LearnerConceptMastery.objects.filter(
            workspace=workspace,
            learner=learner,
        )
    }
    wrong_ids = tuple(
        WrongQuestionRecord.objects.filter(
            workspace=workspace,
            learner=learner,
            status=WrongQuestionRecord.Status.OPEN,
            item_id__in=current_ids,
        )
        .order_by("item_id")
        .values_list("item_id", flat=True)
    )
    return AssessmentSelectionContext(
        mastery_by_concept=masteries,
        open_wrong_item_ids=wrong_ids,
    )


def _increment_mastery(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    concept_id: str,
    correct: bool,
) -> None:
    record, _ = LearnerConceptMastery.objects.select_for_update().get_or_create(
        workspace=workspace,
        learner=learner,
        concept_id=concept_id,
    )
    attempted = record.attempted_count + 1
    correct_count = record.correct_count + int(correct)
    mastery = min(correct_count / attempted, 0.9)
    record.attempted_count = attempted
    record.correct_count = correct_count
    record.attempt_status = LearnerConceptMastery.AttemptStatus.ATTEMPTED
    record.mastery = Decimal(str(mastery)).quantize(Decimal("0.001"))
    record.save(
        update_fields=(
            "attempted_count",
            "correct_count",
            "attempt_status",
            "mastery",
            "updated_at",
        )
    )


def _revise_mastery(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    concept_id: str,
    correct_delta: int,
) -> None:
    record = LearnerConceptMastery.objects.select_for_update().get(
        workspace=workspace,
        learner=learner,
        concept_id=concept_id,
    )
    corrected = record.correct_count + correct_delta
    if corrected < 0 or corrected > record.attempted_count:
        raise DomainError(
            code="ASSESSMENT_PROJECTION_CONFLICT",
            module="m0",
            message="复核后的知识点计数无法安全更新。",
        )
    record.correct_count = corrected
    record.mastery = Decimal(
        str(min(corrected / record.attempted_count, 0.9))
    ).quantize(Decimal("0.001"))
    record.save(update_fields=("correct_count", "mastery", "updated_at"))


def _update_wrong_ledger(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    item_id: str,
    item_version: str,
    attempt_id: str,
    correct: bool,
) -> None:
    record = WrongQuestionRecord.objects.select_for_update().filter(
        workspace=workspace,
        learner=learner,
        item_id=item_id,
    ).first()
    if correct:
        if record is not None:
            record.item_version = item_version
            record.latest_attempt_id = attempt_id
            record.status = WrongQuestionRecord.Status.RESOLVED
            record.save(
                update_fields=(
                    "item_version",
                    "latest_attempt_id",
                    "status",
                    "updated_at",
                )
            )
        return
    if record is None:
        WrongQuestionRecord.objects.create(
            workspace=workspace,
            learner=learner,
            item_id=item_id,
            item_version=item_version,
            latest_attempt_id=attempt_id,
            status=WrongQuestionRecord.Status.OPEN,
        )
        return
    record.item_version = item_version
    record.latest_attempt_id = attempt_id
    record.wrong_count += 1
    record.status = WrongQuestionRecord.Status.OPEN
    record.save(
        update_fields=(
            "item_version",
            "latest_attempt_id",
            "wrong_count",
            "status",
            "updated_at",
        )
    )


def _latest_audits(scoring: ScoringResultBundle):
    latest = {}
    for audit in scoring.score_audit_records:
        current = latest.get(audit.item_instance_id)
        if current is None or audit.audit_version > current.audit_version:
            latest = {**latest, audit.item_instance_id: audit}
    return latest


def _validate_projection_scope(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    task: TaskPlan,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
) -> None:
    if (
        task.course_id != course_id
        or task.class_id != class_id
        or task.learner_id != learner.actor_id
        or paper.task_id != task.task_id
        or paper.learner_id != learner.actor_id
        or scoring.paper_id != paper.paper_id
        or scoring.learner_id != learner.actor_id
    ):
        raise DomainError(
            code="ASSESSMENT_PROJECTION_SCOPE_MISMATCH",
            module="m0",
            message="评分结果与课程、班级或学生不一致。",
        )


__all__ = [
    "project_finalized_assessment",
    "selection_context_for_learner",
]
