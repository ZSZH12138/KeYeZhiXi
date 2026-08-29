"""Validate, preview, or atomically apply the canonical role seed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from course_insight.infrastructure.config import (
    RoleGrantSeed,
    RoleSeedError,
    RoleSyncPlan,
    build_role_sync_plan,
    load_role_seeds,
)
from course_insight.modules.m0_platform.django_app.authz import (
    PERMISSION_CODENAMES,
    ROLE_PERMISSIONS,
)
from course_insight.modules.m0_platform.django_app.models import (
    AccountType,
    ActorGrant,
    RoleSyncState,
    User,
)


_SOURCE_NAME = "roles"
_APP_LABEL = "m0_platform_web"


class Command(BaseCommand):
    """Synchronize validated pseudonymous grants into M0 Web tables."""

    help = "Validate, preview, or atomically apply config/roles.csv"

    def add_arguments(self, parser: Any) -> None:
        modes = parser.add_mutually_exclusive_group(required=True)
        modes.add_argument(
            "--check",
            action="store_true",
            help="Validate the canonical seed without database changes.",
        )
        modes.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Preview the database diff without changing it.",
        )
        modes.add_argument(
            "--apply",
            action="store_true",
            help="Apply the database diff atomically.",
        )

    def handle(self, *args: object, **options: object) -> None:
        try:
            document = _load_configured_document()
            if options["check"]:
                self.stdout.write(
                    _safe_summary(
                        mode="check",
                        checksum=document.checksum,
                        grant_count=len(document.grants),
                    )
                )
                return
            if options["dry_run"]:
                plan = build_role_sync_plan(
                    document,
                    _current_seeds(at=timezone.now()),
                    mode="dry-run",
                )
                self.stdout.write(_safe_plan_summary(plan))
                return
            plan, changed = _apply_document(document)
        except CommandError:
            raise
        except RoleSeedError as exc:
            raise CommandError(
                f"role seed validation failed: {exc.reason}"
            ) from None
        except (DatabaseError, IntegrityError, DjangoValidationError):
            raise CommandError("role synchronization failed") from None
        except (AttributeError, TypeError, ValueError):
            raise CommandError("role seed configuration is invalid") from None

        self.stdout.write(
            _safe_plan_summary(plan, changed=changed)
        )


def _load_configured_document():
    try:
        config_root = Path(settings.COURSE_INSIGHT_CONFIG_DIR)
        configured_path = Path(settings.COURSE_INSIGHT_ROLES_PATH)
    except (AttributeError, TypeError):
        raise CommandError("role seed configuration is unavailable") from None
    document = load_role_seeds(
        configured_path,
        config_root=config_root,
    )
    # The CSV is now a recovery bootstrap only. Legacy rows remain parseable
    # so upgrades can diagnose old files, but they never create live accounts.
    return type(document)(
        grants=tuple(
            seed for seed in document.grants if seed.role == "system_admin"
        ),
        checksum=document.checksum,
    )


def _apply_document(document):
    moment = timezone.now()
    with transaction.atomic():
        state, state_created = (
            RoleSyncState.objects.select_for_update().get_or_create(
                source_name=_SOURCE_NAME,
                defaults={
                    "source_checksum": document.checksum,
                    "grant_count": len(document.grants),
                    "synchronized_at": moment,
                },
            )
        )
        current_seeds = _current_seeds(at=moment, lock=True)
        plan = build_role_sync_plan(
            document,
            current_seeds,
            mode="apply",
        )
        desired_identities = {grant.identity for grant in document.grants}
        groups_changed = _synchronize_group_permissions()
        grants_changed = _apply_grants(
            document.grants,
            source_checksum=document.checksum,
            at=moment,
        )
        grants_changed = (
            _revoke_omitted_grants(
                tuple(
                    grant
                    for grant in plan.revoke
                    if grant.identity not in desired_identities
                ),
                source_checksum=document.checksum,
                at=moment,
            )
            or grants_changed
        )
        membership_changed = _synchronize_memberships(
            actor_ids={
                *(seed.actor_id for seed in document.grants),
                *(seed.actor_id for seed in plan.revoke),
            },
            at=moment,
        )
        metadata_changed = (
            state.source_checksum != document.checksum
            or state.grant_count != len(document.grants)
        )
        changed = (
            state_created
            or plan.has_changes
            or groups_changed
            or grants_changed
            or membership_changed
            or metadata_changed
        )
        if not state_created and changed:
            state.source_checksum = document.checksum
            state.grant_count = len(document.grants)
            state.synchronized_at = moment
            state.save(
                update_fields=(
                    "source_checksum",
                    "grant_count",
                    "synchronized_at",
                )
            )
    return plan, changed


def _current_seeds(
    *,
    at,
    lock: bool = False,
) -> tuple[RoleGrantSeed, ...]:
    queryset = ActorGrant.objects.select_related("user").order_by(
        "user__actor_id",
        "role",
        "course_id",
        "class_id",
    )
    if lock:
        queryset = queryset.select_for_update()
    return tuple(
        RoleGrantSeed(
            actor_id=grant.user.actor_id,
            role=grant.role,  # type: ignore[arg-type]
            course_id=grant.course_id,
            class_id=grant.class_id,
            is_active=_is_effective(grant, at=at),
        )
        for grant in queryset
    )


def _is_effective(grant: ActorGrant, *, at) -> bool:
    return (
        grant.is_active
        and grant.revoked_at is None
        and grant.valid_from <= at
        and (grant.valid_until is None or grant.valid_until > at)
    )


def _synchronize_group_permissions() -> bool:
    permissions = {
        permission.codename: permission
        for permission in Permission.objects.filter(
            content_type__app_label=_APP_LABEL,
            codename__in=PERMISSION_CODENAMES,
        )
    }
    if set(permissions) != set(PERMISSION_CODENAMES):
        raise CommandError(
            "role permissions are unavailable; apply Django migrations"
        )
    changed = False
    for role, codenames in ROLE_PERMISSIONS.items():
        group, created = Group.objects.get_or_create(name=role)
        expected = {permissions[codename].pk for codename in codenames}
        current = set(group.permissions.values_list("pk", flat=True))
        if current != expected:
            group.permissions.set(sorted(expected))
            changed = True
        changed = changed or created
    return changed


def _apply_grants(
    seeds: tuple[RoleGrantSeed, ...],
    *,
    source_checksum: str,
    at,
) -> bool:
    changed = False
    for seed in seeds:
        user = User.objects.select_for_update().filter(
            actor_id=seed.actor_id
        ).first()
        user_created = False
        if user is None:
            if seed.role != "system_admin":
                # Live teacher/student accounts are owned by the account
                # administrator UI. Legacy rows must never resurrect them.
                continue
            user, user_created = _get_or_create_user(seed.actor_id)
        expected_type = (
            AccountType.ADMINISTRATOR
            if seed.role == "system_admin"
            else AccountType.TEACHER
            if seed.role in {"teacher", "course_admin"}
            else AccountType.STUDENT
        )
        if user.account_type != expected_type:
            user.account_type = expected_type
            user.save(update_fields=("account_type",))
            changed = True
        grant = (
            ActorGrant.objects.select_for_update()
            .filter(
                user=user,
                role=seed.role,
                course_id=seed.course_id,
                class_id=seed.class_id,
            )
            .first()
        )
        if grant is None:
            ActorGrant.objects.create(
                user=user,
                role=seed.role,
                course_id=seed.course_id,
                class_id=seed.class_id,
                is_active=seed.is_active,
                source_checksum=source_checksum,
                valid_from=at,
                valid_until=None,
                revoked_at=None if seed.is_active else at,
            )
            changed = True
            continue

        effective = _is_effective(grant, at=at)
        update_fields: list[str] = []
        if grant.source_checksum != source_checksum:
            grant.source_checksum = source_checksum
            update_fields.append("source_checksum")
        if seed.is_active and not effective:
            grant.is_active = True
            grant.valid_from = at
            grant.valid_until = None
            grant.revoked_at = None
            update_fields.extend(
                ("is_active", "valid_from", "valid_until", "revoked_at")
            )
        elif not seed.is_active and effective:
            grant.is_active = False
            grant.revoked_at = at
            update_fields.extend(("is_active", "revoked_at"))
        elif not seed.is_active and grant.revoked_at is None:
            grant.is_active = False
            grant.revoked_at = at
            update_fields.extend(("is_active", "revoked_at"))
        if update_fields:
            grant.save(update_fields=tuple(dict.fromkeys(update_fields)))
            changed = True
        changed = changed or user_created
    return changed


def _revoke_omitted_grants(
    seeds: tuple[RoleGrantSeed, ...],
    *,
    source_checksum: str,
    at,
) -> bool:
    changed = False
    for seed in seeds:
        user = User.objects.select_for_update().filter(
            actor_id=seed.actor_id
        ).first()
        if user is None:
            continue
        grant = (
            ActorGrant.objects.select_for_update()
            .filter(
                user=user,
                role=seed.role,
                course_id=seed.course_id,
                class_id=seed.class_id,
            )
            .first()
        )
        if grant is None:
            continue
        effective = _is_effective(grant, at=at)
        update_fields: list[str] = []
        if grant.source_checksum != source_checksum:
            grant.source_checksum = source_checksum
            update_fields.append("source_checksum")
        if effective or grant.is_active or grant.revoked_at is None:
            grant.is_active = False
            grant.revoked_at = at
            update_fields.extend(("is_active", "revoked_at"))
        if update_fields:
            grant.save(update_fields=tuple(dict.fromkeys(update_fields)))
            changed = True
    return changed


def _get_or_create_user(actor_id: str) -> tuple[User, bool]:
    user = User.objects.select_for_update().filter(
        actor_id=actor_id
    ).first()
    if user is not None:
        return user, False
    user = User(
        username=actor_id,
        actor_id=actor_id,
        email="",
        first_name="",
        last_name="",
        is_active=True,
        account_type=AccountType.ADMINISTRATOR,
    )
    user.set_unusable_password()
    user.full_clean()
    user.save()
    return user, True


def _synchronize_memberships(
    *,
    actor_ids: set[str],
    at,
) -> bool:
    if not actor_ids:
        return False
    groups = {
        group.name: group
        for group in Group.objects.filter(name__in=ROLE_PERMISSIONS)
    }
    changed = False
    users = User.objects.select_for_update().filter(
        actor_id__in=sorted(actor_ids)
    )
    for user in users:
        active_roles = set(
            ActorGrant.objects.filter(
                user=user,
                is_active=True,
                revoked_at__isnull=True,
                valid_from__lte=at,
            )
            .filter(Q(valid_until__isnull=True) | Q(valid_until__gt=at))
            .values_list("role", flat=True)
        )
        if len(active_roles) > 1:
            raise RoleSeedError(
                fields=("roles.role",),
                reason="conflicting_role",
            )
        current = set(
            user.groups.filter(name__in=ROLE_PERMISSIONS).values_list(
                "name",
                flat=True,
            )
        )
        if current == active_roles:
            continue
        user.groups.remove(
            *[
                groups[role]
                for role in sorted(current)
                if role in groups
            ]
        )
        user.groups.add(
            *[
                groups[role]
                for role in sorted(active_roles)
                if role in groups
            ]
        )
        changed = True
    return changed


def _safe_summary(
    *,
    mode: str,
    checksum: str,
    grant_count: int,
) -> str:
    return (
        f"roles mode={mode} valid=true grants={grant_count} "
        f"checksum={checksum}"
    )


def _safe_plan_summary(
    plan: RoleSyncPlan,
    *,
    changed: bool | None = None,
) -> str:
    fields = [
        f"roles mode={plan.mode}",
        f"create={len(plan.create)}",
        f"activate={len(plan.activate)}",
        f"revoke={len(plan.revoke)}",
        f"unchanged={len(plan.unchanged)}",
        f"checksum={plan.source_checksum}",
    ]
    if changed is not None:
        fields.append(f"changed={str(changed).lower()}")
    return " ".join(fields)
