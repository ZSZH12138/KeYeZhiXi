import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("m0_platform_web", "0010_view_course_files_permission"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScopedDeepSeekConfiguration",
            fields=[
                (
                    "workspace",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="deepseek_configuration",
                        serialize=False,
                        to="m0_platform_web.courseclassworkspace",
                    ),
                ),
                (
                    "encrypted_api_key",
                    models.BinaryField(blank=True, null=True),
                ),
                (
                    "encryption_scheme",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                (
                    "masked_key",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                (
                    "model_name",
                    models.CharField(
                        default="deepseek-v4-flash",
                        max_length=64,
                    ),
                ),
                ("thinking_enabled", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="scoped_deepseek_updates",
                        to="m0_platform_web.user",
                    ),
                ),
            ],
        )
    ]
