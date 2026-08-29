"""Exact-scope Django adapter for active student rosters."""

from __future__ import annotations

from datetime import datetime

from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone

from course_insight.application.class_roster import ClassRosterSnapshot
from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    RoleName,
)


def capture_active_class_roster(
    *,
    course_id: str,
    class_id: str,
    captured_at: datetime | None = None,
) -> ClassRosterSnapshot:
    """Capture enabled accounts with one currently effective student grant."""

    moment = timezone.now() if captured_at is None else captured_at
    try:
        learner_ids = tuple(
            ActorGrant.objects.filter(
                user__is_active=True,
                role=RoleName.STUDENT,
                course_id=course_id,
                class_id=class_id,
                is_active=True,
                revoked_at__isnull=True,
                valid_from__lte=moment,
            )
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=moment))
            .order_by("user__actor_id")
            .values_list("user__actor_id", flat=True)
            .distinct()
        )
    except DatabaseError as error:
        raise DomainError(
            code="CLASS_ROSTER_UNAVAILABLE",
            module="m0",
            message="class roster is unavailable",
            recoverable=True,
        ) from error
    return ClassRosterSnapshot.capture(
        course_id=course_id,
        class_id=class_id,
        learner_ids=learner_ids,
        captured_at=moment,
    )


def capture_profile_class_roster(
    *,
    course_id: str,
    class_id: str,
    learner_id: str,
    captured_at: datetime | None = None,
) -> ClassRosterSnapshot:
    """Require a non-empty roster containing the profile-changing learner."""

    snapshot = capture_active_class_roster(
        course_id=course_id,
        class_id=class_id,
        captured_at=captured_at,
    )
    if snapshot.active_student_count < 1:
        raise DomainError(
            code="CLASS_ROSTER_INVALID",
            module="m0",
            message="profile-changing class roster is empty",
            recoverable=True,
        )
    if not snapshot.contains(learner_id):
        raise DomainError(
            code="ASSESSMENT_SCOPE_MISMATCH",
            module="m0",
            message="learner is outside the active class roster",
            recoverable=False,
        )
    return snapshot


__all__ = ["capture_active_class_roster", "capture_profile_class_roster"]
