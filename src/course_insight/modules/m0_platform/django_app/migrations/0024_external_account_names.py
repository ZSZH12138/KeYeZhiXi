"""Separate unrestricted external account names from internal actor IDs."""

from __future__ import annotations

import hashlib

import course_insight.modules.m0_platform.django_app.models
from django.db import migrations, models


def _digest(account_name: str) -> str:
    return hashlib.sha256(
        b"course-insight-account-name\0" + account_name.encode("utf-8")
    ).hexdigest()


def backfill_username_digests(apps, schema_editor) -> None:
    del schema_editor
    User = apps.get_model("m0_platform_web", "User")
    for user in User.objects.only("pk", "username").iterator():
        User.objects.filter(pk=user.pk).update(
            username_digest=_digest(user.username)
        )


def clear_username_digests(apps, schema_editor) -> None:
    del schema_editor
    User = apps.get_model("m0_platform_web", "User")
    User.objects.update(username_digest=None)


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("m0_platform_web", "0023_backfill_account_authorization"),
    ]

    operations = [
        migrations.AlterModelManagers(
            name="user",
            managers=[
                (
                    "objects",
                    course_insight.modules.m0_platform.django_app.models.AccountUserManager(),
                ),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="user",
            name="m0_user_identity_fields_safe",
        ),
        migrations.AddField(
            model_name="user",
            name="username_digest",
            field=models.CharField(
                editable=False,
                max_length=64,
                null=True,
            ),
        ),
        migrations.RunPython(
            backfill_username_digests,
            clear_username_digests,
        ),
        migrations.AlterField(
            model_name="user",
            name="username_digest",
            field=models.CharField(
                editable=False,
                max_length=64,
                unique=True,
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="username",
            field=models.TextField(),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("email", ""),
                    ("first_name", ""),
                    ("last_name", ""),
                ),
                name="m0_user_identity_fields_safe",
            ),
        ),
    ]
