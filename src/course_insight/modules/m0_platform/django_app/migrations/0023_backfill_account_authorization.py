"""Repair role-group permissions for accounts created before class governance."""

from django.db import migrations


_APP_LABEL = "m0_platform_web"
_TEACHER_PERMISSIONS = frozenset(
    {
        "view_class_analytics",
        "view_student_report",
        "review_score",
        "configure_deepseek",
        "manage_course_knowledge",
        "open_class",
        "manage_class_members",
    }
)
_ADMINISTRATOR_PERMISSIONS = frozenset({"manage_accounts"})


def _ensure_group_permissions(Group, Permission, *, role, codenames):
    permissions = {
        permission.codename: permission
        for permission in Permission.objects.filter(
            content_type__app_label=_APP_LABEL,
            codename__in=codenames,
        )
    }
    missing = set(codenames) - set(permissions)
    if missing:
        return None
    group, _ = Group.objects.get_or_create(name=role)
    expected_ids = sorted(permission.pk for permission in permissions.values())
    if set(group.permissions.values_list("pk", flat=True)) != set(expected_ids):
        group.permissions.set(expected_ids)
    return group


def _backfill_group_for_account_type(
    User,
    Group,
    Permission,
    *,
    account_type,
    role,
    codenames,
):
    users = User.objects.filter(account_type=account_type)
    group = _ensure_group_permissions(
        Group,
        Permission,
        role=role,
        codenames=codenames,
    )
    if group is None:
        # Django creates model permissions in post_migrate, after RunPython.
        # A fresh install cannot have legacy users to repair, so it can safely
        # wait for normal role synchronization/account creation.  An upgrade
        # with affected accounts must fail rather than leave them half-set up.
        if users.exists():
            raise RuntimeError(
                "required account authorization permissions are unavailable"
            )
        return
    for user in users.iterator():
        user.groups.add(group)


def backfill_account_authorization(apps, schema_editor):
    """Give pre-existing global accounts the same groups as UI-created ones.

    Historical ``course_admin`` grants were intentionally folded into the
    teacher account type by migration 0020.  They therefore receive the
    canonical teacher group too, while keeping their legacy grant data for
    existing course content and audit compatibility.
    """

    del schema_editor
    User = apps.get_model("m0_platform_web", "User")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    _backfill_group_for_account_type(
        User,
        Group,
        Permission,
        account_type="teacher",
        role="teacher",
        codenames=_TEACHER_PERMISSIONS,
    )
    _backfill_group_for_account_type(
        User,
        Group,
        Permission,
        account_type="administrator",
        role="system_admin",
        codenames=_ADMINISTRATOR_PERMISSIONS,
    )


def reverse_backfill_account_authorization(apps, schema_editor):
    del apps, schema_editor


class Migration(migrations.Migration):
    dependencies = [
        ("m0_platform_web", "0022_scoped_deepseek_updated_by_set_null"),
    ]

    operations = [
        migrations.RunPython(
            backfill_account_authorization,
            reverse_backfill_account_authorization,
        ),
    ]
