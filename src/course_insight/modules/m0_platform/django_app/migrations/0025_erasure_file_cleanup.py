"""Persist safe retry work for physical account-erasure file cleanup."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("m0_platform_web", "0024_external_account_names"),
    ]

    operations = [
        migrations.CreateModel(
            name="ErasureFileCleanup",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("knowledge_upload", "Knowledge upload"),
                            ("frozen_submission", "Frozen submission"),
                        ],
                        max_length=32,
                    ),
                ),
                ("opaque_key", models.CharField(max_length=128)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                (
                    "last_error_code",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("queued_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="erasurefilecleanup",
            constraint=models.UniqueConstraint(
                fields=("kind", "opaque_key"),
                name="m0_erasure_file_cleanup_unique_key",
            ),
        ),
        migrations.AddIndex(
            model_name="erasurefilecleanup",
            index=models.Index(
                fields=["kind", "queued_at"],
                name="m0_erase_file_queue_idx",
            ),
        ),
    ]
