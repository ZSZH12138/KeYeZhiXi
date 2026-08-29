"""Transactional projection from finalized scores to simple learner records."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Sequence

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from course_insight.contracts.tasking import PROFILE_AFFECTING_TASK_TYPES

from course_insight.contracts.assessment import AssessmentPaper, ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
    ClassLearningSnapshot,
    CourseClassWorkspace,
    LearnerConceptMastery,
    LearningProfileProjectionEvent,
    ReleaseQuestion,
    User,
    WrongQuestionRecord,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    synchronize_class_learning_snapshot,
    synchronize_profile_projection,
)
from course_insight.modules.m8_assessment_scoring.selection_policy import (
    AssessmentSelectionContext,
    ConceptMastery,
)


PROFILE_TASK_TYPES = PROFILE_AFFECTING_TASK_TYPES


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
    if task.task_type not in PROFILE_TASK_TYPES:
        return False
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
        changed_concept_ids = _changed_concept_ids(
            previous=existing.projection_payload,
            current=payload,
        )
        previous_checksum = existing.scoring_checksum
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
        _synchronize_profile_change(
            workspace=existing.workspace,
            learner=learner,
            attempt_id=scoring.attempt_id,
            task=task,
            previous_scoring_checksum=previous_checksum,
            scoring_checksum=checksum,
            changed_concept_ids=changed_concept_ids,
            reason="revision",
        )
        return True

    workspace, _ = CourseClassWorkspace.objects.get_or_create(
        course_id=course_id,
        class_id=class_id,
    )
    for item in paper.all_items():
        correct = bool(payload[item.item_instance_id]["correct"])
        if not correct:
            _record_profile_wrong(
                workspace=workspace,
                learner=learner,
                item_id=item.item_id,
                item_version=item.item_version,
                attempt_id=scoring.attempt_id,
                paper_id=paper.paper_id,
                item_instance_id=item.item_instance_id,
            )
        if task.task_type in PROFILE_TASK_TYPES:
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
    _synchronize_profile_change(
        workspace=workspace,
        learner=learner,
        attempt_id=scoring.attempt_id,
        task=task,
        previous_scoring_checksum=None,
        scoring_checksum=checksum,
        changed_concept_ids=_all_concept_ids(payload),
        reason="initial",
    )
    return True


@transaction.atomic
def record_finalized_correction(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    task: TaskPlan,
    paper: AssessmentPaper,
    scoring: ScoringResultBundle,
) -> bool:
    """Resolve current wrong-item state without creating profile evidence."""

    _validate_projection_scope(
        course_id=course_id,
        class_id=class_id,
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    if task.task_type != "correction":
        return False
    if scoring.requires_teacher_review() or scoring.has_rejected_score():
        return False
    payload = _projection_payload(paper, scoring)
    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
    ).first()
    if workspace is None:
        return False
    changed = False
    for item in paper.all_items():
        if not bool(payload[item.item_instance_id]["correct"]):
            continue
        record = WrongQuestionRecord.objects.select_for_update().filter(
            workspace=workspace,
            learner=learner,
            status=WrongQuestionRecord.Status.OPEN,
        ).filter(
            Q(
                active_follow_up_paper_id=paper.paper_id,
                active_follow_up_item_id=item.item_id,
            )
            | Q(item_id=item.item_id, active_follow_up_paper_id="")
        ).first()
        if record is None:
            continue
        update_fields = [
            "active_follow_up_paper_id",
            "active_follow_up_item_id",
            "updated_at",
        ]
        record.active_follow_up_paper_id = ""
        record.active_follow_up_item_id = ""
        if bool(payload[item.item_instance_id]["correct"]):
            record.status = WrongQuestionRecord.Status.RESOLVED
            record.resolved_through_attempt_id = record.latest_attempt_id
            record.resolved_at = timezone.now()
            update_fields.extend(
                ("status", "resolved_through_attempt_id", "resolved_at")
            )
            changed = True
        record.save(update_fields=tuple(update_fields))
    return changed


@transaction.atomic
def link_correction_follow_up(
    *,
    course_id: str,
    class_id: str,
    learner: User,
    source_paper_id: str,
    source_item_instance_id: str,
    source_item_id: str,
    follow_up_paper_id: str,
    follow_up_item_id: str,
) -> None:
    """Bind one temporary correction paper to its current wrong-item state."""

    record = WrongQuestionRecord.objects.select_for_update().filter(
        workspace__course_id=course_id,
        workspace__class_id=class_id,
        learner=learner,
        item_id=source_item_id,
        status=WrongQuestionRecord.Status.OPEN,
    ).first()
    if record is None:
        raise DomainError(
            code="CORRECTION_ITEM_NOT_OPEN",
            module="m0",
            message="该题当前不需要订正。",
            recoverable=True,
        )
    record.source_paper_id = source_paper_id
    record.source_item_instance_id = source_item_instance_id
    record.active_follow_up_paper_id = follow_up_paper_id
    record.active_follow_up_item_id = follow_up_item_id
    record.save(
        update_fields=(
            "source_paper_id",
            "source_item_instance_id",
            "active_follow_up_paper_id",
            "active_follow_up_item_id",
            "updated_at",
        )
    )


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
            "stem": item.stem,
            "concept_ids": list(item.concept_ids),
            "cause": "；".join(
                score.reason
                for score in audit.criterion_scores
                if score.reason
            )
            or "本题未得到满分。",
            "correct": math.isclose(
                audit.total_score,
                audit.max_score,
                rel_tol=0.0,
                abs_tol=1e-9,
            ),
        }
    return payload


def correction_records_view(
    *,
    course_id: str,
    class_id: str,
    learner_id: str | None = None,
) -> dict[str, dict[str, object]]:
    """Build legacy-shaped correction presentation from the DB source of truth."""

    rows = WrongQuestionRecord.objects.select_related(
        "learner",
        "workspace__active_release",
    ).filter(
        workspace__course_id=course_id,
        workspace__class_id=class_id,
    )
    if learner_id is not None:
        rows = rows.filter(learner__actor_id=learner_id)
    row_list = tuple(rows.order_by("learner__actor_id", "item_id"))
    receipts = {
        receipt.attempt_id: receipt
        for receipt in AssessmentProjectionReceipt.objects.filter(
            attempt_id__in=tuple(row.latest_attempt_id for row in row_list)
        )
    }
    fallback_stems = {
        (release_id, question_id): stem
        for release_id, question_id, stem in ReleaseQuestion.objects.filter(
            release_id__in=tuple(
                {
                    row.workspace.active_release_id
                    for row in row_list
                    if row.workspace.active_release_id is not None
                }
            ),
            question_id__in=tuple({row.item_id for row in row_list}),
        ).values_list("release_id", "question_id", "stem")
    }
    records: dict[str, dict[str, object]] = {}
    for row in row_list:
        receipt = receipts.get(row.latest_attempt_id)
        paper_id = row.source_paper_id or (
            receipt.paper_id if receipt is not None else row.latest_attempt_id
        )
        instance_id = row.source_item_instance_id or row.item_id
        evidence: object = None
        if receipt is not None and isinstance(receipt.projection_payload, dict):
            evidence = receipt.projection_payload.get(instance_id)
            if not isinstance(evidence, dict):
                evidence = next(
                    (
                        item
                        for item in receipt.projection_payload.values()
                        if isinstance(item, dict)
                        and item.get("item_id") == row.item_id
                    ),
                    None,
                )
        details = evidence if isinstance(evidence, dict) else {}
        paper_record = records.setdefault(
            paper_id,
            {
                "learner_id": row.learner.actor_id,
                "course_id": course_id,
                "class_id": class_id,
                "items": {},
            },
        )
        items = paper_record["items"]
        if isinstance(items, dict):
            items[instance_id] = {
                "stem": str(
                    details.get("stem")
                    or fallback_stems.get(
                        (row.workspace.active_release_id, row.item_id),
                        "",
                    )
                ),
                "cause": str(details.get("cause") or "本题未得到满分。"),
                "concept_ids": list(details.get("concept_ids") or []),
                "hint_revealed": row.hint_revealed,
                "follow_up_correct": (
                    row.status == WrongQuestionRecord.Status.RESOLVED
                ),
            }
    return records


@transaction.atomic
def rebuild_authoritative_profile_state() -> dict[str, int]:
    """Rebuild M0/M5 presentation state only from formal profile receipts.

    Existing correction completion and hint state is preserved only when the
    item can still be traced to diagnostic or stage-assessment evidence.
    """

    non_profile_receipts = AssessmentProjectionReceipt.objects.exclude(
        task_type__in=PROFILE_TASK_TYPES
    )
    removed_receipts = non_profile_receipts.count()
    non_profile_receipts.delete()
    receipts = tuple(
        AssessmentProjectionReceipt.objects.select_related(
            "workspace",
            "learner",
        ).order_by("applied_at", "pk")
    )
    normalized: list[
        tuple[AssessmentProjectionReceipt, tuple[tuple[str, dict[str, object]], ...]]
    ] = []
    for receipt in receipts:
        payload = receipt.projection_payload
        if not isinstance(payload, dict):
            raise DomainError(
                code="ASSESSMENT_PROJECTION_CONFLICT",
                module="m0",
                message="历史画像投影格式无效，不能安全重建。",
            )
        items: list[tuple[str, dict[str, object]]] = []
        for instance_id, raw_item in payload.items():
            if (
                not isinstance(instance_id, str)
                or not isinstance(raw_item, dict)
                or not isinstance(raw_item.get("item_id"), str)
                or not isinstance(raw_item.get("item_version"), str)
                or not isinstance(raw_item.get("concept_ids"), list)
                or any(
                    not isinstance(concept_id, str)
                    for concept_id in raw_item.get("concept_ids", [])
                )
            ):
                raise DomainError(
                    code="ASSESSMENT_PROJECTION_CONFLICT",
                    module="m0",
                    message="历史画像题目格式无效，不能安全重建。",
                )
            items.append((instance_id, raw_item))
        normalized.append((receipt, tuple(items)))

    preserved = {
        (row.workspace_id, row.learner_id, row.item_id): {
            "status": row.status,
            "hint_revealed": row.hint_revealed,
            "resolved_at": row.resolved_at,
            "resolved_through_attempt_id": row.resolved_through_attempt_id,
        }
        for row in WrongQuestionRecord.objects.all()
    }
    LearningProfileProjectionEvent.objects.all().delete()
    ClassLearningSnapshot.objects.all().delete()
    LearnerConceptMastery.objects.all().delete()
    WrongQuestionRecord.objects.all().delete()

    for receipt, items in normalized:
        for instance_id, item in items:
            correct = bool(item.get("correct"))
            for concept_id in item["concept_ids"]:
                _increment_mastery(
                    workspace=receipt.workspace,
                    learner=receipt.learner,
                    concept_id=str(concept_id),
                    correct=correct,
                )
            if not correct:
                _record_profile_wrong(
                    workspace=receipt.workspace,
                    learner=receipt.learner,
                    item_id=str(item["item_id"]),
                    item_version=str(item["item_version"]),
                    attempt_id=receipt.attempt_id,
                    paper_id=receipt.paper_id,
                    item_instance_id=instance_id,
                )

    for row in WrongQuestionRecord.objects.all():
        state = preserved.get((row.workspace_id, row.learner_id, row.item_id))
        if state is None:
            continue
        row.hint_revealed = bool(state["hint_revealed"])
        update_fields = ["hint_revealed", "updated_at"]
        if state["status"] == WrongQuestionRecord.Status.RESOLVED:
            row.status = WrongQuestionRecord.Status.RESOLVED
            row.resolved_at = state["resolved_at"] or timezone.now()
            row.resolved_through_attempt_id = str(
                state["resolved_through_attempt_id"] or row.latest_attempt_id
            )
            update_fields.extend(
                ("status", "resolved_at", "resolved_through_attempt_id")
            )
        row.save(update_fields=tuple(update_fields))

    for workspace in CourseClassWorkspace.objects.all().order_by("course_id", "class_id"):
        synchronize_class_learning_snapshot(workspace=workspace, reason="rebuild")
    return {
        "profile_receipts": len(receipts),
        "removed_non_profile_receipts": removed_receipts,
        "mastery_records": LearnerConceptMastery.objects.count(),
        "wrong_question_records": WrongQuestionRecord.objects.count(),
    }


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
        if task.task_type in PROFILE_TASK_TYPES:
            for concept_id in item["concept_ids"]:
                _revise_mastery(
                    workspace=workspace,
                    learner=learner,
                    concept_id=str(concept_id),
                    correct_delta=int(is_correct) - int(was_correct),
                )
        if is_correct:
            _resolve_revised_profile_wrong(
                workspace=workspace,
                learner=learner,
                item_id=str(item["item_id"]),
                item_version=str(item["item_version"]),
                attempt_id=attempt_id,
            )
        else:
            _record_profile_wrong(
                workspace=workspace,
                learner=learner,
                item_id=str(item["item_id"]),
                item_version=str(item["item_version"]),
                attempt_id=attempt_id,
            )


def _synchronize_profile_change(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    attempt_id: str,
    task: TaskPlan,
    previous_scoring_checksum: str | None,
    scoring_checksum: str,
    changed_concept_ids: Sequence[str],
    reason: str,
) -> None:
    """Send finalized profile changes through the M0 → M5 → M9 snapshot path."""

    if task.task_type not in PROFILE_TASK_TYPES or not changed_concept_ids:
        return
    synchronize_profile_projection(
        workspace=workspace,
        learner=learner,
        attempt_id=attempt_id,
        task_type=task.task_type,
        previous_scoring_checksum=previous_scoring_checksum,
        scoring_checksum=scoring_checksum,
        changed_concept_ids=changed_concept_ids,
        reason=reason,
    )


def _all_concept_ids(
    payload: dict[str, dict[str, object]],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(concept_id)
                for item in payload.values()
                for concept_id in item["concept_ids"]
            }
        )
    )


def _changed_concept_ids(
    *,
    previous: object,
    current: dict[str, dict[str, object]],
) -> tuple[str, ...]:
    if not isinstance(previous, dict):
        return ()
    changed: set[str] = set()
    for instance_id, item in current.items():
        before = previous.get(instance_id)
        if not isinstance(before, dict):
            continue
        if bool(before.get("correct")) != bool(item["correct"]):
            changed.update(str(value) for value in item["concept_ids"])
    return tuple(sorted(changed))


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


def _record_profile_wrong(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    item_id: str,
    item_version: str,
    attempt_id: str,
    paper_id: str = "",
    item_instance_id: str = "",
) -> None:
    record = WrongQuestionRecord.objects.select_for_update().filter(
        workspace=workspace,
        learner=learner,
        item_id=item_id,
    ).first()
    if record is None:
        WrongQuestionRecord.objects.create(
            workspace=workspace,
            learner=learner,
            item_id=item_id,
            item_version=item_version,
            latest_attempt_id=attempt_id,
            source_paper_id=paper_id,
            source_item_instance_id=item_instance_id,
            status=WrongQuestionRecord.Status.OPEN,
        )
        return
    record.item_version = item_version
    record.latest_attempt_id = attempt_id
    record.source_paper_id = paper_id
    record.source_item_instance_id = item_instance_id
    record.wrong_count += 1
    record.status = WrongQuestionRecord.Status.OPEN
    record.save(
        update_fields=(
            "item_version",
            "latest_attempt_id",
            "source_paper_id",
            "source_item_instance_id",
            "wrong_count",
            "status",
            "updated_at",
        )
    )


def _resolve_revised_profile_wrong(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    item_id: str,
    item_version: str,
    attempt_id: str,
) -> None:
    record = WrongQuestionRecord.objects.select_for_update().filter(
        workspace=workspace,
        learner=learner,
        item_id=item_id,
        latest_attempt_id=attempt_id,
    ).first()
    if record is None:
        return
    record.item_version = item_version
    record.status = WrongQuestionRecord.Status.RESOLVED
    record.resolved_through_attempt_id = attempt_id
    record.resolved_at = timezone.now()
    record.save(
        update_fields=(
            "item_version",
            "status",
            "resolved_through_attempt_id",
            "resolved_at",
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
    "correction_records_view",
    "link_correction_follow_up",
    "project_finalized_assessment",
    "rebuild_authoritative_profile_state",
    "record_finalized_correction",
    "selection_context_for_learner",
]
