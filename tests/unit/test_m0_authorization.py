from __future__ import annotations

import hashlib
import hmac
import os
from datetime import timedelta

import django
import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models.query import QuerySet
from django.utils import timezone


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app.authz import (  # noqa: E402
    authorize_scope,
    is_login_allowed,
    register_login_failure,
    reset_login_failures,
)
from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    ActorGrant,
    LoginFailureBucket,
    User,
)


pytestmark = pytest.mark.django_db
APP_LABEL = "m0_platform_web"


def _user_with_permission(
    actor_id: str,
    *,
    role: str,
    permission: str,
) -> User:
    user = User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
    )
    group, _ = Group.objects.get_or_create(name=role)
    django_permission = Permission.objects.get(
        content_type__app_label=APP_LABEL,
        codename=permission,
    )
    group.permissions.add(django_permission)
    user.groups.add(group)
    return user


def _grant(
    user: User,
    *,
    role: str,
    course_id: str | None,
    class_id: str | None,
    **updates: object,
) -> ActorGrant:
    values = {
        "user": user,
        "role": role,
        "course_id": course_id,
        "class_id": class_id,
        "source_checksum": "a" * 64,
        **updates,
    }
    return ActorGrant.objects.create(**values)


def test_user_requires_a_pseudonymous_actor_id_without_real_identity() -> None:
    user = User(
        username="pseudonym_student_001",
        actor_id="pseudonym_student_001",
        email="",
        first_name="",
        last_name="",
    )
    user.set_unusable_password()
    user.full_clean()
    user.set_password("temporary-test-password")

    assert user.check_password("temporary-test-password")
    assert user.password != "temporary-test-password"

    invalid = User(
        username="not-pseudonymous",
        actor_id="real.person@example.com",
    )
    invalid.set_unusable_password()
    with pytest.raises(ValidationError):
        invalid.full_clean()

    real_identity = User(
        username="pseudonym_student_002",
        actor_id="pseudonym_student_002",
        email="real.person@example.com",
    )
    real_identity.set_unusable_password()
    with pytest.raises(ValidationError):
        real_identity.full_clean()

    mismatched_login = User(
        username="pseudonym_login_alias",
        actor_id="pseudonym_student_003",
    )
    mismatched_login.set_unusable_password()
    with pytest.raises(ValidationError):
        mismatched_login.full_clean()


def test_teacher_scope_uses_an_exact_course_class_pair() -> None:
    user = _user_with_permission(
        "pseudonym_teacher_001",
        role="teacher",
        permission="view_student_report",
    )
    _grant(
        user,
        role="teacher",
        course_id="course_a",
        class_id="class_1",
    )
    _grant(
        user,
        role="teacher",
        course_id="course_b",
        class_id="class_2",
    )

    context = authorize_scope(
        user,
        "view_student_report",
        course_id="course_a",
        class_id="class_1",
        learner_id="pseudonym_student_001",
    )

    assert context.actor_id == user.actor_id
    assert context.role == "teacher"
    assert context.course_ids == ["course_a"]
    assert context.class_ids == ["class_1"]

    with pytest.raises(PermissionDenied):
        authorize_scope(
            user,
            "view_student_report",
            course_id="course_a",
            class_id="class_2",
            learner_id="pseudonym_student_001",
        )


def test_permission_and_database_grant_are_both_required() -> None:
    missing_permission = User.objects.create_user(
        username="pseudonym_teacher_no_permission",
        actor_id="pseudonym_teacher_no_permission",
    )
    _grant(
        missing_permission,
        role="teacher",
        course_id="course_a",
        class_id="class_1",
    )
    with pytest.raises(PermissionDenied):
        authorize_scope(
            missing_permission,
            "review_score",
            course_id="course_a",
            class_id="class_1",
        )

    missing_grant = _user_with_permission(
        "pseudonym_teacher_no_grant",
        role="teacher",
        permission="review_score",
    )
    with pytest.raises(PermissionDenied):
        authorize_scope(
            missing_grant,
            "review_score",
            course_id="course_a",
            class_id="class_1",
        )


@pytest.mark.parametrize(
    "grant_updates",
    [
        {"is_active": False, "revoked_at": timezone.now()},
        {
            "valid_from": timezone.now() - timedelta(days=2),
            "valid_until": timezone.now() - timedelta(days=1),
        },
        {"valid_from": timezone.now() + timedelta(days=1)},
    ],
)
def test_revoked_expired_or_not_yet_valid_grants_are_denied(
    grant_updates: dict[str, object],
) -> None:
    suffix = str(abs(hash(tuple(sorted(grant_updates)))))
    user = _user_with_permission(
        f"pseudonym_teacher_{suffix}",
        role="teacher",
        permission="review_score",
    )
    _grant(
        user,
        role="teacher",
        course_id="course_a",
        class_id="class_1",
        **grant_updates,
    )

    with pytest.raises(PermissionDenied):
        authorize_scope(
            user,
            "review_score",
            course_id="course_a",
            class_id="class_1",
        )


def test_student_can_only_access_their_own_learner_scope() -> None:
    user = _user_with_permission(
        "pseudonym_student_001",
        role="student",
        permission="view_own_result",
    )
    _grant(
        user,
        role="student",
        course_id="course_a",
        class_id="class_1",
    )

    authorize_scope(
        user,
        "view_own_result",
        course_id="course_a",
        class_id="class_1",
        learner_id=user.actor_id,
    )
    with pytest.raises(PermissionDenied):
        authorize_scope(
            user,
            "view_own_result",
            course_id="course_a",
            class_id="class_1",
            learner_id="pseudonym_student_002",
        )


@pytest.mark.parametrize(
    "permission",
    ["submit_assessment", "view_own_result", "view_own_feedback"],
)
def test_student_sensitive_permissions_require_explicit_own_learner_id(
    permission: str,
) -> None:
    user = _user_with_permission(
        f"pseudonym_student_{permission}",
        role="student",
        permission=permission,
    )
    _grant(
        user,
        role="student",
        course_id="course_a",
        class_id="class_1",
    )

    with pytest.raises(PermissionDenied):
        authorize_scope(
            user,
            permission,
            course_id="course_a",
            class_id="class_1",
        )


def test_multiple_active_student_scopes_fail_closed_at_authorization() -> None:
    user = _user_with_permission(
        "pseudonym_student_drifted",
        role="student",
        permission="view_own_result",
    )
    _grant(
        user,
        role="student",
        course_id="course_a",
        class_id="class_1",
    )
    _grant(
        user,
        role="student",
        course_id="course_b",
        class_id="class_2",
    )

    with pytest.raises(PermissionDenied):
        authorize_scope(
            user,
            "view_own_result",
            course_id="course_a",
            class_id="class_1",
            learner_id=user.actor_id,
        )


def test_course_admin_and_system_admin_respect_role_scope() -> None:
    course_admin = _user_with_permission(
        "pseudonym_course_admin_001",
        role="course_admin",
        permission="manage_course_roles",
    )
    _grant(
        course_admin,
        role="course_admin",
        course_id="course_a",
        class_id=None,
    )
    context = authorize_scope(
        course_admin,
        "manage_course_roles",
        course_id="course_a",
        class_id="class_9",
    )
    assert context.course_ids == ["course_a"]
    assert context.class_ids == ["class_9"]
    with pytest.raises(PermissionDenied):
        authorize_scope(
            course_admin,
            "manage_course_roles",
            course_id="course_b",
        )

    system_admin = _user_with_permission(
        "pseudonym_system_admin_001",
        role="system_admin",
        permission="manage_platform",
    )
    _grant(
        system_admin,
        role="system_admin",
        course_id=None,
        class_id=None,
    )
    with pytest.raises(PermissionDenied):
        authorize_scope(
            system_admin,
            "manage_platform",
        )


def test_cross_role_or_ambiguous_active_grants_fail_closed() -> None:
    user = _user_with_permission(
        "pseudonym_ambiguous_001",
        role="teacher",
        permission="view_student_report",
    )
    _grant(
        user,
        role="teacher",
        course_id="course_a",
        class_id="class_1",
    )
    _grant(
        user,
        role="course_admin",
        course_id="course_a",
        class_id=None,
    )

    with pytest.raises(PermissionDenied):
        authorize_scope(
            user,
            "view_student_report",
            course_id="course_a",
            class_id="class_1",
        )


def test_database_constraints_reject_invalid_role_scope() -> None:
    user = User.objects.create_user(
        username="pseudonym_invalid_scope_001",
        actor_id="pseudonym_invalid_scope_001",
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _grant(
                user,
                role="student",
                course_id="course_a",
                class_id=None,
            )


def test_login_failure_bucket_locks_at_threshold_without_raw_identity() -> None:
    now = timezone.now()
    kwargs = {
        "actor_hint": "pseudonym_student_001",
        "client_ip": "192.0.2.10",
        "secret": "test-secret-that-is-never-persisted",
        "limit": 2,
        "window_seconds": 60,
    }

    assert register_login_failure(**kwargs, at=now) is True
    assert register_login_failure(**kwargs, at=now) is False
    assert (
        is_login_allowed(
            actor_hint=kwargs["actor_hint"],
            client_ip=kwargs["client_ip"],
            secret=kwargs["secret"],
            at=now,
        )
        is False
    )

    buckets = tuple(
        LoginFailureBucket.objects.order_by("bucket_key").all()
    )
    assert len(buckets) == 3
    for bucket in buckets:
        persisted = (
            bucket.bucket_key
            + str(bucket.failure_count)
            + str(bucket.locked_until)
        )
        assert kwargs["actor_hint"] not in persisted
        assert kwargs["client_ip"] not in persisted
        assert len(bucket.bucket_key) == 64


def test_login_failure_window_resets_and_success_clears_bucket() -> None:
    now = timezone.now()
    kwargs = {
        "actor_hint": "unknown-login-value",
        "client_ip": "2001:db8::1",
        "secret": "test-secret-that-is-never-persisted",
        "limit": 2,
        "window_seconds": 30,
    }
    assert register_login_failure(**kwargs, at=now) is True
    assert register_login_failure(**kwargs, at=now) is False

    later = now + timedelta(seconds=31)
    assert register_login_failure(**kwargs, at=later) is True
    assert list(
        LoginFailureBucket.objects.order_by("bucket_key").values_list(
            "failure_count",
            flat=True,
        )
    ) == [1, 1, 1]

    reset_login_failures(
        actor_hint=kwargs["actor_hint"],
        client_ip=kwargs["client_ip"],
        secret=kwargs["secret"],
    )
    assert LoginFailureBucket.objects.count() == 0
    assert (
        is_login_allowed(
            actor_hint=kwargs["actor_hint"],
            client_ip=kwargs["client_ip"],
            secret=kwargs["secret"],
            at=later,
        )
        is True
    )


def test_login_failure_limit_also_applies_per_client_ip() -> None:
    now = timezone.now()
    shared_ip = "198.51.100.44"
    common = {
        "client_ip": shared_ip,
        "secret": "development-secret-key-value",
        "limit": 2,
        "window_seconds": 60,
    }

    assert (
        register_login_failure(
            actor_hint="pseudonym_student_alpha",
            at=now,
            **common,
        )
        is True
    )
    assert (
        register_login_failure(
            actor_hint="pseudonym_student_beta",
            at=now,
            **common,
        )
        is False
    )
    assert (
        is_login_allowed(
            actor_hint="pseudonym_student_gamma",
            client_ip=shared_ip,
            secret=common["secret"],
            at=now,
        )
        is False
    )


def test_login_failure_limit_also_applies_to_actor_across_client_ips() -> None:
    now = timezone.now()
    actor_hint = "pseudonym_student_distributed_target"
    secret = "development-secret-key-value"
    common = {
        "actor_hint": actor_hint,
        "secret": secret,
        "limit": 2,
        "window_seconds": 60,
        "at": now,
    }

    assert register_login_failure(client_ip="192.0.2.31", **common) is True
    assert register_login_failure(client_ip="198.51.100.31", **common) is False
    assert (
        is_login_allowed(
            actor_hint=actor_hint,
            client_ip="203.0.113.31",
            secret=secret,
            at=now,
        )
        is False
    )


def test_login_allowance_preserves_existing_actor_ip_bucket_keys() -> None:
    now = timezone.now()
    actor_hint = "pseudonym_student_existing"
    client_ip = "192.0.2.77"
    secret = "development-secret-key-value"
    actor_bytes = actor_hint.encode("utf-8")
    ip_bytes = client_ip.encode("ascii")
    legacy_payload = (
        len(actor_bytes).to_bytes(4, "big")
        + actor_bytes
        + len(ip_bytes).to_bytes(2, "big")
        + ip_bytes
    )
    bucket_key = hmac.new(
        secret.encode("utf-8"),
        legacy_payload,
        hashlib.sha256,
    ).hexdigest()
    LoginFailureBucket.objects.create(
        bucket_key=bucket_key,
        window_started_at=now,
        failure_count=2,
        locked_until=now + timedelta(seconds=60),
    )

    assert (
        is_login_allowed(
            actor_hint=actor_hint,
            client_ip=client_ip,
            secret=secret,
            at=now,
        )
        is False
    )


def test_reset_login_failures_clears_shared_client_ip_bucket() -> None:
    now = timezone.now()
    shared_ip = "198.51.100.45"
    secret = "development-secret-key-value"
    assert (
        register_login_failure(
            actor_hint="pseudonym_student_alpha",
            client_ip=shared_ip,
            secret=secret,
            limit=1,
            window_seconds=120,
            at=now,
        )
        is False
    )

    reset_login_failures(
        actor_hint="pseudonym_student_alpha",
        client_ip=shared_ip,
        secret=secret,
    )

    assert (
        is_login_allowed(
            actor_hint="pseudonym_student_beta",
            client_ip=shared_ip,
            secret=secret,
            at=now,
        )
        is True
    )


def test_reset_login_failures_can_preserve_shared_client_ip_bucket() -> None:
    now = timezone.now()
    shared_ip = "198.51.100.49"
    secret = "development-secret-key-value"
    common = {
        "client_ip": shared_ip,
        "secret": secret,
        "limit": 2,
        "window_seconds": 120,
    }
    assert (
        register_login_failure(
            actor_hint="pseudonym_student_alpha",
            at=now,
            **common,
        )
        is True
    )

    reset_login_failures(
        actor_hint="pseudonym_student_alpha",
        client_ip=shared_ip,
        secret=secret,
        clear_shared_ip=False,
    )

    assert LoginFailureBucket.objects.count() == 1
    assert (
        register_login_failure(
            actor_hint="pseudonym_student_beta",
            at=now,
            **common,
        )
        is False
    )
    assert (
        is_login_allowed(
            actor_hint="pseudonym_student_gamma",
            client_ip=shared_ip,
            secret=secret,
            at=now,
        )
        is False
    )


def test_login_failure_multi_bucket_update_rolls_back_on_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get_or_create = QuerySet.get_or_create
    call_count = 0

    def fail_second_update(
        query_set: QuerySet[LoginFailureBucket],
        *args: object,
        **kwargs: object,
    ) -> tuple[LoginFailureBucket, bool]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise DatabaseError("simulated aggregate bucket failure")
        return original_get_or_create(query_set, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get_or_create", fail_second_update)

    allowed = register_login_failure(
        actor_hint="pseudonym_student_atomic",
        client_ip="198.51.100.46",
        secret="development-secret-key-value",
        limit=3,
        window_seconds=120,
        at=timezone.now(),
    )

    assert allowed is False
    assert LoginFailureBucket.objects.count() == 0


def test_login_allowance_fails_closed_on_database_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_bucket_read(
        query_set: QuerySet[LoginFailureBucket],
        *args: object,
        **kwargs: object,
    ) -> QuerySet[LoginFailureBucket]:
        del query_set, args, kwargs
        raise DatabaseError("simulated bucket read failure")

    monkeypatch.setattr(QuerySet, "values_list", fail_bucket_read)

    assert (
        is_login_allowed(
            actor_hint="pseudonym_student_db_error",
            client_ip="198.51.100.47",
            secret="development-secret-key-value",
            at=timezone.now(),
        )
        is False
    )


def test_login_reset_fails_closed_on_database_delete_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_bucket_delete(
        query_set: QuerySet[LoginFailureBucket],
    ) -> tuple[int, dict[str, int]]:
        del query_set
        raise DatabaseError("simulated bucket delete failure")

    monkeypatch.setattr(QuerySet, "delete", fail_bucket_delete)

    with pytest.raises(PermissionDenied) as exc_info:
        reset_login_failures(
            actor_hint="pseudonym_student_db_error",
            client_ip="198.51.100.48",
            secret="development-secret-key-value",
        )

    assert str(exc_info.value) == ""
