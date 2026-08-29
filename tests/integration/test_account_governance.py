from __future__ import annotations

import django
import pytest
from django.apps import apps
from django.db import IntegrityError, transaction


if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    AccountType,
    ClassMembership,
    CourseClassWorkspace,
    User,
)


pytestmark = pytest.mark.django_db(transaction=True)


def _user(actor_id: str, account_type: str) -> User:
    return User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        account_type=account_type,
        password="Strong-password-123!",
    )


def test_governance_models_enforce_one_account_type_and_active_membership() -> None:
    teacher = _user("pseudonym_teacher_schema", AccountType.TEACHER)
    student = _user("pseudonym_student_schema", AccountType.STUDENT)
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_schema",
        class_id="class_schema",
        course_display_name="课程",
        class_display_name="一班",
        owner_teacher=teacher,
    )
    ClassMembership.objects.create(
        workspace=workspace,
        student=student,
        added_by_teacher=teacher,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClassMembership.objects.create(
                workspace=workspace,
                student=student,
                added_by_teacher=teacher,
            )

    assert workspace.owner_teacher_id == teacher.pk
    assert workspace.course_display_name == "课程"
    assert workspace.class_display_name == "一班"
