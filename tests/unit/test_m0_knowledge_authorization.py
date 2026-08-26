from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied

from course_insight.modules.m0_platform.django_app.authz import (
    authorize_course_knowledge,
)
from course_insight.modules.m0_platform.django_app.models import ActorGrant, User


pytestmark = pytest.mark.django_db


def _authorized_user(actor_id: str, role: str, course_id: str | None, class_id: str | None) -> User:
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    group, _ = Group.objects.get_or_create(name=f"{role}-knowledge")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="manage_course_knowledge",
        )
    )
    user.groups.add(group)
    ActorGrant.objects.create(
        user=user,
        role=role,
        course_id=course_id,
        class_id=class_id,
        source_checksum="a" * 64,
    )
    return user


@pytest.mark.parametrize(
    ("role", "course_id", "class_id"),
    [
        ("teacher", "course_1", "class_1"),
        ("course_admin", "course_1", None),
        ("system_admin", None, None),
    ],
)
def test_course_knowledge_authorizes_governing_roles(role, course_id, class_id) -> None:
    user = _authorized_user(
        f"pseudonym_{role}_knowledge",
        role,
        course_id,
        class_id,
    )

    context = authorize_course_knowledge(user, "course_1")

    assert context.role == role
    assert context.course_ids == ["course_1"]


def test_course_knowledge_denies_cross_course_teacher() -> None:
    user = _authorized_user(
        "pseudonym_teacher_cross_course",
        "teacher",
        "course_1",
        "class_1",
    )

    with pytest.raises(PermissionDenied):
        authorize_course_knowledge(user, "course_2")
