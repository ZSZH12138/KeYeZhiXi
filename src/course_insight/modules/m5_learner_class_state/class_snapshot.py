"""Persistent M5 class snapshots built from the active roster and M0 mastery.

The snapshot is deliberately a derived view: an M0 profile change emits an
idempotent event and rebuilds the affected class view from compact learner
mastery counters.  Reading the current roster again also repairs snapshots
after a student is added, removed, or a knowledge release changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from django.db import transaction
from course_insight.modules.m0_platform.django_app.class_roster import (
    capture_active_class_roster,
)

from course_insight.modules.m0_platform.django_app.models import (
    ClassConceptLearningSnapshot,
    ClassLearningSnapshot,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    LearnerConceptMastery,
    LearningProfileProjectionEvent,
    ReleaseConcept,
    User,
)


_PRIORITY_SUPPORT_THRESHOLD = Decimal("0.400")
_SNAPSHOT_QUANTUM = Decimal("0.001")


@dataclass(frozen=True, slots=True)
class ClassConceptSnapshotView:
    """Teacher-safe named aggregate for one currently published concept."""

    concept_id: str
    name: str
    attempted_student_count: int
    unattempted_student_count: int
    attempted_item_count: int
    correct_item_count: int
    average_mastery: float
    priority_support_count: int
    priority_support_rate: float


@dataclass(frozen=True, slots=True)
class ClassLearningSnapshotView:
    """Teacher-safe projection consumed by M9 presentation code."""

    snapshot_id: str
    course_id: str
    class_id: str
    active_student_count: int
    concepts: tuple[ClassConceptSnapshotView, ...]


def synchronize_class_learning_snapshot(
    *,
    workspace: CourseClassWorkspace,
    reason: str,
) -> ClassLearningSnapshotView:
    """Build or reuse the exact class snapshot for the current source state."""

    with transaction.atomic():
        locked = CourseClassWorkspace.objects.select_for_update().get(
            pk=workspace.pk
        )
        snapshot = _synchronize_locked(locked, reason=reason)
        return _snapshot_view(snapshot)


def synchronize_profile_projection(
    *,
    workspace: CourseClassWorkspace,
    learner: User,
    attempt_id: str,
    task_type: str,
    previous_scoring_checksum: str | None,
    scoring_checksum: str,
    changed_concept_ids: Sequence[str],
    reason: str,
) -> ClassLearningSnapshotView:
    """Record one profile transition and synchronously refresh M5/M9 input.

    The `(workspace, attempt, scoring checksum)` key makes retrying a web
    request safe.  A revision gets a different checksum and therefore creates
    a new immutable event rather than adding the old contribution again.
    """

    with transaction.atomic():
        locked = CourseClassWorkspace.objects.select_for_update().get(
            pk=workspace.pk
        )
        event, _ = LearningProfileProjectionEvent.objects.get_or_create(
            workspace=locked,
            attempt_id=attempt_id,
            scoring_checksum=scoring_checksum,
            defaults={
                "learner": learner,
                "task_type": task_type,
                "previous_scoring_checksum": previous_scoring_checksum,
                "changed_concept_ids": sorted({str(item) for item in changed_concept_ids}),
                "reason": reason,
            },
        )
        if event.learner_id != learner.pk or event.task_type != task_type:
            raise ValueError("profile projection event scope conflicts")
        if event.snapshot_id is None:
            snapshot = _synchronize_locked(locked, reason=reason)
            event.snapshot = snapshot
            event.save(update_fields=("snapshot",))
        else:
            snapshot = event.snapshot
            if snapshot is None:  # pragma: no cover - defensive ORM narrowing
                snapshot = _synchronize_locked(locked, reason=reason)
        return _snapshot_view(snapshot)


def current_class_learning_snapshot(
    *,
    course_id: str,
    class_id: str,
) -> ClassLearningSnapshotView | None:
    """Return the last persisted M5 class snapshot without changing state."""

    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
    ).first()
    if workspace is None:
        return None
    snapshot = (
        ClassLearningSnapshot.objects.filter(workspace=workspace)
        .order_by("-created_at", "-snapshot_id")
        .first()
    )
    return None if snapshot is None else _snapshot_view(snapshot)


def ensure_current_class_learning_snapshot(
    *,
    course_id: str,
    class_id: str,
) -> ClassLearningSnapshotView | None:
    """Repair stale class data caused by roster or release changes on read."""

    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
    ).first()
    if workspace is None:
        return None
    return synchronize_class_learning_snapshot(
        workspace=workspace,
        reason="read_repair",
    )


def _synchronize_locked(
    workspace: CourseClassWorkspace,
    *,
    reason: str,
) -> ClassLearningSnapshot:
    release = _current_release(workspace)
    roster_snapshot = capture_active_class_roster(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    )
    roster = roster_snapshot.learner_ids
    concepts = _release_concepts(release)
    mastery_rows = _mastery_rows(
        workspace=workspace,
        learner_ids=roster,
        concept_ids=tuple(concepts),
    )
    roster_checksum = roster_snapshot.roster_checksum
    input_checksum = _checksum(
        {
            "release_id": None if release is None else str(release.pk),
            "release_checksum": None if release is None else release.content_checksum,
            "roster": roster,
            "masteries": [
                {
                    "learner_id": row.learner_id,
                    "concept_id": row.concept_id,
                    "attempted": row.attempted_count,
                    "correct": row.correct_count,
                    "status": row.attempt_status,
                    "mastery": str(row.mastery),
                }
                for row in mastery_rows
            ],
        }
    )
    existing = ClassLearningSnapshot.objects.filter(
        workspace=workspace,
        input_checksum=input_checksum,
    ).first()
    if existing is not None:
        return existing

    snapshot = ClassLearningSnapshot.objects.create(
        workspace=workspace,
        release=release,
        input_checksum=input_checksum,
        roster_checksum=roster_checksum,
        active_student_count=len(roster),
        reason=reason[:32] or "sync",
    )
    by_concept: dict[str, list[LearnerConceptMastery]] = {
        concept_id: [] for concept_id in concepts
    }
    for row in mastery_rows:
        by_concept[row.concept_id].append(row)
    ClassConceptLearningSnapshot.objects.bulk_create(
        [
            _concept_snapshot_row(
                snapshot=snapshot,
                concept=concept,
                roster_size=len(roster),
                records=by_concept[concept.concept_id],
            )
            for concept in concepts.values()
        ]
    )
    return snapshot


def _current_release(
    workspace: CourseClassWorkspace,
) -> CourseKnowledgeRelease | None:
    active = workspace.active_release
    if (
        active is not None
        and active.course_id == workspace.course_id
        and active.class_id == workspace.class_id
        and active.status == CourseKnowledgeRelease.Status.ACTIVE
    ):
        return active
    return (
        CourseKnowledgeRelease.objects.filter(
            course_id=workspace.course_id,
            class_id=workspace.class_id,
            status=CourseKnowledgeRelease.Status.ACTIVE,
        )
        .order_by("-version_number")
        .first()
    )


def _release_concepts(
    release: CourseKnowledgeRelease | None,
) -> dict[str, ReleaseConcept]:
    if release is None:
        return {}
    return {
        concept.concept_id: concept
        for concept in ReleaseConcept.objects.filter(release=release).order_by(
            "concept_id"
        )
    }


def _mastery_rows(
    *,
    workspace: CourseClassWorkspace,
    learner_ids: Sequence[str],
    concept_ids: Sequence[str],
) -> tuple[LearnerConceptMastery, ...]:
    if not learner_ids or not concept_ids:
        return ()
    return tuple(
        LearnerConceptMastery.objects.filter(
            workspace=workspace,
            learner__actor_id__in=learner_ids,
            concept_id__in=concept_ids,
            attempt_status=LearnerConceptMastery.AttemptStatus.ATTEMPTED,
            attempted_count__gt=0,
        ).order_by("concept_id", "learner_id")
    )


def _concept_snapshot_row(
    *,
    snapshot: ClassLearningSnapshot,
    concept: ReleaseConcept,
    roster_size: int,
    records: Sequence[LearnerConceptMastery],
) -> ClassConceptLearningSnapshot:
    attempted_students = len(records)
    attempted_items = sum(record.attempted_count for record in records)
    correct_items = sum(record.correct_count for record in records)
    support_count = sum(
        1
        for record in records
        if Decimal(record.mastery) < _PRIORITY_SUPPORT_THRESHOLD
    )
    mean = (
        Decimal("0")
        if attempted_students == 0
        else sum((Decimal(record.mastery) for record in records), Decimal("0"))
        / Decimal(attempted_students)
    )
    support_rate = (
        Decimal("0")
        if attempted_students == 0
        else Decimal(support_count) / Decimal(attempted_students)
    )
    return ClassConceptLearningSnapshot(
        snapshot=snapshot,
        concept_id=concept.concept_id,
        concept_name=concept.name,
        attempted_student_count=attempted_students,
        unattempted_student_count=max(roster_size - attempted_students, 0),
        attempted_item_count=attempted_items,
        correct_item_count=correct_items,
        average_mastery=_quantize(mean),
        priority_support_count=support_count,
        priority_support_rate=_quantize(support_rate),
    )


def _snapshot_view(snapshot: ClassLearningSnapshot) -> ClassLearningSnapshotView:
    return ClassLearningSnapshotView(
        snapshot_id=str(snapshot.snapshot_id),
        course_id=snapshot.workspace.course_id,
        class_id=snapshot.workspace.class_id,
        active_student_count=snapshot.active_student_count,
        concepts=tuple(
            ClassConceptSnapshotView(
                concept_id=row.concept_id,
                name=row.concept_name,
                attempted_student_count=row.attempted_student_count,
                unattempted_student_count=row.unattempted_student_count,
                attempted_item_count=row.attempted_item_count,
                correct_item_count=row.correct_item_count,
                average_mastery=float(row.average_mastery),
                priority_support_count=row.priority_support_count,
                priority_support_rate=float(row.priority_support_rate),
            )
            for row in snapshot.concepts.order_by("concept_id")
        ),
    )


def _checksum(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_SNAPSHOT_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "ClassConceptSnapshotView",
    "ClassLearningSnapshotView",
    "current_class_learning_snapshot",
    "ensure_current_class_learning_snapshot",
    "synchronize_class_learning_snapshot",
    "synchronize_profile_projection",
]
