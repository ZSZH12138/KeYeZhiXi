"""Teacher-owned course/class creation and active-roster write service."""

from __future__ import annotations

import hashlib
import hmac
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from course_insight.application.class_roster import ClassRosterSnapshot
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.class_roster import (
    capture_active_class_roster,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    User,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    synchronize_class_learning_snapshot,
)


def _normalized_name(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 255:
        raise ValidationError("课程名或班级名无效")
    return normalized


def _creation_digest(teacher: User, request_token: str) -> str:
    if not isinstance(request_token, str) or not 16 <= len(request_token) <= 128:
        raise ValidationError("开课请求标识无效")
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        b"course-insight-open-class\0"
        + str(teacher.pk).encode("ascii")
        + b"\0"
        + request_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _authorize_teacher(teacher: User, permission: str) -> None:
    if (
        not teacher.is_authenticated
        or not teacher.is_active
        or teacher.account_type != AccountType.TEACHER
        or not teacher.has_perm(f"m0_platform_web.{permission}")
    ):
        raise PermissionDenied


@transaction.atomic
def create_owned_workspace(
    *,
    teacher: User,
    course_name: str,
    class_name: str,
    request_token: str,
) -> CourseClassWorkspace:
    """Create exactly one independent teacher-owned class per request token."""

    _authorize_teacher(teacher, "open_class")
    digest = _creation_digest(teacher, request_token)
    existing = CourseClassWorkspace.objects.select_for_update().filter(
        creation_token_digest=digest
    ).first()
    if existing is not None:
        if existing.owner_teacher_id != teacher.pk:
            raise PermissionDenied
        return existing
    workspace = CourseClassWorkspace.objects.create(
        course_id=f"course_{uuid.uuid4().hex}",
        class_id=f"class_{uuid.uuid4().hex}",
        course_display_name=_normalized_name(course_name),
        class_display_name=_normalized_name(class_name),
        owner_teacher=teacher,
        creation_token_digest=digest,
    )
    transaction.on_commit(runtime.close_web_runtime)
    return workspace


@transaction.atomic
def add_student(
    *,
    workspace: CourseClassWorkspace,
    teacher: User,
    actor_id: str,
) -> ClassRosterSnapshot:
    """Add one existing student account to the teacher's active class."""

    locked = _locked_owned_workspace(workspace, teacher)
    student = User.objects.filter(
        actor_id=actor_id,
        account_type=AccountType.STUDENT,
        is_active=True,
    ).first()
    if student is None:
        raise ValidationError("未找到有效的学生账户")
    if ClassMembership.objects.select_for_update().filter(
        workspace=locked,
        student=student,
        status=ClassMembership.Status.ACTIVE,
        removed_at__isnull=True,
    ).exists():
        return _snapshot(locked)
    ClassMembership.objects.create(
        workspace=locked,
        student=student,
        added_by_teacher=teacher,
    )
    _roster_changed(locked)
    return _snapshot(locked)


@transaction.atomic
def remove_student(
    *,
    workspace: CourseClassWorkspace,
    teacher: User,
    actor_id: str,
) -> ClassRosterSnapshot:
    """End one active membership without deleting the student's account."""

    locked = _locked_owned_workspace(workspace, teacher)
    membership = (
        ClassMembership.objects.select_for_update()
        .filter(
            workspace=locked,
            student__actor_id=actor_id,
            status=ClassMembership.Status.ACTIVE,
            removed_at__isnull=True,
        )
        .first()
    )
    if membership is None:
        raise ValidationError("该学生当前不在班级中")
    membership.status = ClassMembership.Status.REMOVED
    membership.removed_at = timezone.now()
    membership.removed_by_teacher = teacher
    membership.save(
        update_fields=("status", "removed_at", "removed_by_teacher")
    )
    _roster_changed(locked)
    return _snapshot(locked)


def _locked_owned_workspace(
    workspace: CourseClassWorkspace,
    teacher: User,
) -> CourseClassWorkspace:
    _authorize_teacher(teacher, "manage_class_members")
    locked = CourseClassWorkspace.objects.select_for_update().get(pk=workspace.pk)
    if not locked.is_owned_by(teacher):
        raise PermissionDenied
    return locked


def _roster_changed(workspace: CourseClassWorkspace) -> None:
    CourseClassWorkspace.objects.filter(pk=workspace.pk).update(
        roster_version=F("roster_version") + 1
    )
    workspace.refresh_from_db(fields=("roster_version",))
    transaction.on_commit(
        lambda: synchronize_class_learning_snapshot(
            workspace=workspace,
            reason="roster_change",
        )
    )


def _snapshot(workspace: CourseClassWorkspace) -> ClassRosterSnapshot:
    return capture_active_class_roster(
        course_id=workspace.course_id,
        class_id=workspace.class_id,
    )
