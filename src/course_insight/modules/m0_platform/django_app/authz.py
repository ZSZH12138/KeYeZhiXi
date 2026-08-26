"""Centralized two-layer authorization for the M0 Django boundary."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from course_insight.contracts.platform import ActorContext
from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    LoginFailureBucket,
    PSEUDONYMOUS_ACTOR_PATTERN,
    RoleName,
    SCOPE_ID_PATTERN,
    User,
)


ROLE_PERMISSIONS: Final[Mapping[str, frozenset[str]]] = {
    RoleName.STUDENT: frozenset(
        {
            "start_assessment",
            "submit_assessment",
            "view_own_result",
            "view_own_feedback",
            "ask_course_question",
            "view_course_files",
        }
    ),
    RoleName.TEACHER: frozenset(
        {
            "view_class_analytics",
            "view_student_report",
            "review_score",
            "configure_deepseek",
            "manage_course_knowledge",
        }
    ),
    RoleName.COURSE_ADMIN: frozenset(
        {
            "view_class_analytics",
            "view_student_report",
            "review_score",
            "manage_course_roles",
            "configure_deepseek",
            "manage_course_knowledge",
        }
    ),
    RoleName.SYSTEM_ADMIN: frozenset(
        {
            "start_assessment",
            "submit_assessment",
            "view_own_result",
            "view_own_feedback",
            "view_class_analytics",
            "view_student_report",
            "review_score",
            "manage_course_roles",
            "manage_platform",
            "configure_deepseek",
            "manage_course_knowledge",
            "ask_course_question",
            "view_course_files",
        }
    ),
}
PERMISSION_CODENAMES: Final[frozenset[str]] = frozenset(
    permission
    for permissions in ROLE_PERMISSIONS.values()
    for permission in permissions
)
_APP_LABEL = "m0_platform_web"
_STUDENT_SELF_PERMISSIONS = frozenset(
    {
        "submit_assessment",
        "view_own_result",
        "view_own_feedback",
        "ask_course_question",
        "view_course_files",
    }
)
_scope_pattern = re.compile(SCOPE_ID_PATTERN)
_actor_pattern = re.compile(PSEUDONYMOUS_ACTOR_PATTERN)


def authorize_scope(
    user: User | AnonymousUser,
    permission: str,
    *,
    course_id: str | None = None,
    class_id: str | None = None,
    learner_id: str | None = None,
    at: datetime | None = None,
) -> ActorContext:
    """Authorize a target against a Django permission and one exact grant.

    The returned context is intentionally target-scoped. It never combines
    independent course and class lists from multiple grants.
    """

    moment = _validated_moment(at)
    _validate_target(
        permission=permission,
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
    )
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "pk", None) is None
    ):
        raise PermissionDenied
    if not user.has_perm(f"{_APP_LABEL}.{permission}"):
        raise PermissionDenied

    grants = tuple(
        ActorGrant.objects.filter(
            user_id=user.pk,
            is_active=True,
            revoked_at__isnull=True,
            valid_from__lte=moment,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=moment))
        .order_by("role", "course_id", "class_id", "pk")
    )
    roles = {grant.role for grant in grants}
    if len(roles) != 1:
        raise PermissionDenied
    role = next(iter(roles))
    if permission not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise PermissionDenied
    if role == RoleName.STUDENT:
        if len(grants) != 1:
            raise PermissionDenied
        if learner_id is not None and learner_id != user.actor_id:
            raise PermissionDenied
        if (
            permission in _STUDENT_SELF_PERMISSIONS
            and learner_id is None
        ):
            raise PermissionDenied

    matching = tuple(
        grant
        for grant in grants
        if _grant_matches_target(
            grant,
            course_id=course_id,
            class_id=class_id,
        )
    )
    if len(matching) != 1:
        raise PermissionDenied

    return ActorContext(
        actor_id=user.actor_id,
        role=role,
        course_ids=[] if course_id is None else [course_id],
        class_ids=[] if class_id is None else [class_id],
        issued_at=moment,
    )


def authorize_deepseek_config(user: User | AnonymousUser) -> ActorContext:
    """Authorize platform-wide DeepSeek configuration without a class target."""

    moment = _validated_moment(None)
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "pk", None) is None
    ):
        raise PermissionDenied
    if not user.has_perm(f"{_APP_LABEL}.configure_deepseek"):
        raise PermissionDenied
    grants = tuple(
        ActorGrant.objects.filter(
            user_id=user.pk,
            is_active=True,
            revoked_at__isnull=True,
            valid_from__lte=moment,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=moment))
        .order_by("role", "course_id", "class_id", "pk")
    )
    roles = {grant.role for grant in grants}
    if len(roles) != 1:
        raise PermissionDenied
    role = next(iter(roles))
    if "configure_deepseek" not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise PermissionDenied
    return ActorContext(
        actor_id=user.actor_id,
        role=role,
        course_ids=[],
        class_ids=[],
        issued_at=moment,
    )


def authorize_course_knowledge(
    user: User | AnonymousUser,
    course_id: str,
    *,
    at: datetime | None = None,
) -> ActorContext:
    """Authorize teacher administration against one exact course.

    A teacher may have several class grants for the same course; those grants
    represent one course-level management authority and are not treated as an
    ambiguity. Grants from different roles are still rejected fail-closed.
    """

    moment = _validated_moment(at)
    if not isinstance(course_id, str) or not _scope_pattern.fullmatch(course_id):
        raise PermissionDenied
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or getattr(user, "pk", None) is None
        or not user.has_perm(f"{_APP_LABEL}.manage_course_knowledge")
    ):
        raise PermissionDenied

    grants = tuple(
        ActorGrant.objects.filter(
            user_id=user.pk,
            is_active=True,
            revoked_at__isnull=True,
            valid_from__lte=moment,
        )
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=moment))
        .order_by("role", "course_id", "class_id", "pk")
    )
    roles = {grant.role for grant in grants}
    if len(roles) != 1:
        raise PermissionDenied
    role = next(iter(roles))
    if "manage_course_knowledge" not in ROLE_PERMISSIONS.get(role, frozenset()):
        raise PermissionDenied

    if role == RoleName.TEACHER:
        matching = tuple(grant for grant in grants if grant.course_id == course_id)
        if not matching:
            raise PermissionDenied
        class_ids = sorted(
            {grant.class_id for grant in matching if grant.class_id is not None}
        )
    elif role == RoleName.COURSE_ADMIN:
        matching = tuple(
            grant
            for grant in grants
            if grant.course_id == course_id and grant.class_id is None
        )
        if len(matching) != 1:
            raise PermissionDenied
        class_ids = []
    elif role == RoleName.SYSTEM_ADMIN:
        if len(grants) != 1 or grants[0].course_id is not None:
            raise PermissionDenied
        class_ids = []
    else:
        raise PermissionDenied

    return ActorContext(
        actor_id=user.actor_id,
        role=role,
        course_ids=[course_id],
        class_ids=class_ids,
        issued_at=moment,
    )


def _validated_moment(value: datetime | None) -> datetime:
    moment = timezone.now() if value is None else value
    if timezone.is_naive(moment):
        raise PermissionDenied
    return moment


def _validate_target(
    *,
    permission: str,
    course_id: str | None,
    class_id: str | None,
    learner_id: str | None,
) -> None:
    if permission not in PERMISSION_CODENAMES:
        raise PermissionDenied
    if class_id is not None and course_id is None:
        raise PermissionDenied
    if course_id is not None and not _scope_pattern.fullmatch(course_id):
        raise PermissionDenied
    if class_id is not None and not _scope_pattern.fullmatch(class_id):
        raise PermissionDenied
    if learner_id is not None and not _actor_pattern.fullmatch(learner_id):
        raise PermissionDenied


def _grant_matches_target(
    grant: ActorGrant,
    *,
    course_id: str | None,
    class_id: str | None,
) -> bool:
    if grant.role in {RoleName.STUDENT, RoleName.TEACHER}:
        return (
            course_id is not None
            and class_id is not None
            and grant.course_id == course_id
            and grant.class_id == class_id
        )
    if grant.role == RoleName.COURSE_ADMIN:
        return (
            course_id is not None
            and grant.course_id == course_id
            and grant.class_id is None
        )
    return (
        grant.role == RoleName.SYSTEM_ADMIN
        and grant.course_id is None
        and grant.class_id is None
    )


def is_login_allowed(
    *,
    actor_hint: str,
    client_ip: str,
    secret: str,
    at: datetime | None = None,
) -> bool:
    """Return False for an active lock or any database uncertainty."""

    moment = _validated_moment(at)
    bucket_keys = _login_bucket_keys(
        actor_hint=actor_hint,
        client_ip=client_ip,
        secret=secret,
    )
    try:
        locked_untils = tuple(
            LoginFailureBucket.objects.filter(
                bucket_key__in=bucket_keys
            ).values_list("locked_until", flat=True)
        )
    except DatabaseError:
        return False
    return all(
        locked_until is None or locked_until <= moment
        for locked_until in locked_untils
    )


def register_login_failure(
    *,
    actor_hint: str,
    client_ip: str,
    secret: str,
    limit: int,
    window_seconds: int,
    at: datetime | None = None,
) -> bool:
    """Atomically record one failure and return whether another try is allowed."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > 10_000
        or isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds < 1
        or window_seconds > 86_400
    ):
        raise PermissionDenied
    moment = _validated_moment(at)
    bucket_keys = _login_bucket_keys(
        actor_hint=actor_hint,
        client_ip=client_ip,
        secret=secret,
    )
    window = timedelta(seconds=window_seconds)
    try:
        with transaction.atomic():
            locked_untils: list[datetime | None] = []
            for bucket_key in bucket_keys:
                bucket, created = (
                    LoginFailureBucket.objects.select_for_update().get_or_create(
                        bucket_key=bucket_key,
                        defaults={
                            "window_started_at": moment,
                            "failure_count": 1,
                            "locked_until": (
                                moment + window if limit == 1 else None
                            ),
                        },
                    )
                )
                if not created:
                    window_ended = bucket.window_started_at + window
                    if moment >= window_ended:
                        bucket.window_started_at = moment
                        bucket.failure_count = 1
                        bucket.locked_until = (
                            moment + window if limit == 1 else None
                        )
                    else:
                        bucket.failure_count += 1
                        if bucket.failure_count >= limit:
                            bucket.locked_until = window_ended
                    bucket.save(
                        update_fields=(
                            "window_started_at",
                            "failure_count",
                            "locked_until",
                            "updated_at",
                        )
                    )
                locked_untils.append(bucket.locked_until)
    except DatabaseError:
        return False
    return all(
        locked_until is None or locked_until <= moment
        for locked_until in locked_untils
    )


def reset_login_failures(
    *,
    actor_hint: str,
    client_ip: str,
    secret: str,
    clear_shared_ip: bool = True,
) -> None:
    """Clear actor-specific buckets and optionally the shared IP bucket."""

    bucket_keys = _login_bucket_keys(
        actor_hint=actor_hint,
        client_ip=client_ip,
        secret=secret,
    )
    if not clear_shared_ip:
        bucket_keys = bucket_keys[:-1]
    try:
        with transaction.atomic():
            LoginFailureBucket.objects.filter(
                bucket_key__in=bucket_keys
            ).delete()
    except DatabaseError:
        raise PermissionDenied from None


def _login_bucket_keys(
    *,
    actor_hint: str,
    client_ip: str,
    secret: str,
) -> tuple[str, str, str]:
    if (
        not isinstance(actor_hint, str)
        or not actor_hint
        or actor_hint != actor_hint.strip()
        or len(actor_hint) > 256
        or not actor_hint.isprintable()
        or not isinstance(secret, str)
        or len(secret) < 16
    ):
        raise PermissionDenied
    try:
        normalized_ip = ipaddress.ip_address(client_ip).compressed
    except (TypeError, ValueError):
        raise PermissionDenied from None
    return (
        _login_bucket_key(
            actor_hint=actor_hint,
            normalized_ip=normalized_ip,
            secret=secret,
            scope="actor_ip",
        ),
        _login_bucket_key(
            actor_hint=actor_hint,
            normalized_ip=normalized_ip,
            secret=secret,
            scope="actor",
        ),
        _login_bucket_key(
            actor_hint=actor_hint,
            normalized_ip=normalized_ip,
            secret=secret,
            scope="ip",
        ),
    )


def _login_bucket_key(
    *,
    actor_hint: str,
    normalized_ip: str,
    secret: str,
    scope: str,
) -> str:
    if scope not in {"actor_ip", "actor", "ip"}:
        raise PermissionDenied
    actor_bytes = actor_hint.encode("utf-8")
    ip_bytes = normalized_ip.encode("ascii")
    if scope == "actor_ip":
        # Preserve already-persisted actor+IP buckets across this additive
        # upgrade. Only the new IP-only namespace needs domain separation.
        payload = (
            len(actor_bytes).to_bytes(4, "big")
            + actor_bytes
            + len(ip_bytes).to_bytes(2, "big")
            + ip_bytes
        )
    elif scope == "actor":
        scope_bytes = scope.encode("ascii")
        payload = (
            len(scope_bytes).to_bytes(1, "big")
            + scope_bytes
            + len(actor_bytes).to_bytes(4, "big")
            + actor_bytes
        )
    else:
        scope_bytes = scope.encode("ascii")
        payload = (
            len(scope_bytes).to_bytes(1, "big")
            + scope_bytes
            + len(ip_bytes).to_bytes(2, "big")
            + ip_bytes
        )
    return hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
