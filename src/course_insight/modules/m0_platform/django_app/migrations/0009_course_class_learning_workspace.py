from __future__ import annotations

import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def _backfill_class_scope(apps, schema_editor) -> None:
    del schema_editor
    ActorGrant = apps.get_model("m0_platform_web", "ActorGrant")
    CourseSource = apps.get_model("m0_platform_web", "CourseSource")
    KnowledgeIngestionJob = apps.get_model(
        "m0_platform_web", "KnowledgeIngestionJob"
    )
    CourseKnowledgeRelease = apps.get_model(
        "m0_platform_web", "CourseKnowledgeRelease"
    )
    CourseClassWorkspace = apps.get_model(
        "m0_platform_web", "CourseClassWorkspace"
    )

    def class_for(*, user_id, course_id: str) -> str:
        class_ids = list(
            ActorGrant.objects.filter(
                user_id=user_id,
                course_id=course_id,
                class_id__isnull=False,
                is_active=True,
            )
            .order_by("class_id")
            .values_list("class_id", flat=True)
            .distinct()
        )
        return class_ids[0] if len(class_ids) == 1 else "class_1"

    for source in CourseSource.objects.filter(class_id__isnull=True).iterator():
        source.class_id = class_for(
            user_id=source.created_by_id,
            course_id=source.course_id,
        )
        source.save(update_fields=("class_id",))
    for job in KnowledgeIngestionJob.objects.filter(class_id__isnull=True).iterator():
        job.class_id = class_for(
            user_id=job.requested_by_id,
            course_id=job.course_id,
        )
        job.save(update_fields=("class_id",))
    for release in CourseKnowledgeRelease.objects.filter(
        class_id__isnull=True
    ).select_related("job"):
        release.class_id = release.job.class_id
        release.save(update_fields=("class_id",))
    scopes = set(
        CourseSource.objects.values_list("course_id", "class_id")
    ) | set(KnowledgeIngestionJob.objects.values_list("course_id", "class_id"))
    for course_id, class_id in sorted(scopes):
        CourseClassWorkspace.objects.get_or_create(
            course_id=course_id,
            class_id=class_id,
        )


def _set_active_release_pointers(apps, schema_editor) -> None:
    del schema_editor
    CourseKnowledgeRelease = apps.get_model(
        "m0_platform_web", "CourseKnowledgeRelease"
    )
    CourseClassWorkspace = apps.get_model(
        "m0_platform_web", "CourseClassWorkspace"
    )
    for release in CourseKnowledgeRelease.objects.filter(status="active"):
        workspace, _ = CourseClassWorkspace.objects.get_or_create(
            course_id=release.course_id,
            class_id=release.class_id,
        )
        workspace.active_release_id = release.pk
        workspace.content_revision = max(workspace.content_revision, 1)
        workspace.save(update_fields=("active_release", "content_revision"))


class Migration(migrations.Migration):
    dependencies = [("m0_platform_web", "0008_finalize_legacy_deleted_source_versions")]

    operations = [
        migrations.AddField(
            model_name="coursesource",
            name="class_id",
            field=models.CharField(max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="knowledgeingestionjob",
            name="class_id",
            field=models.CharField(max_length=128, null=True),
        ),
        migrations.AddField(
            model_name="courseknowledgerelease",
            name="class_id",
            field=models.CharField(max_length=128, null=True),
        ),
        migrations.CreateModel(
            name="CourseClassWorkspace",
            fields=[
                (
                    "workspace_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "course_id",
                    models.CharField(
                        max_length=128,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="scope identifier is invalid",
                                regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
                            )
                        ],
                    ),
                ),
                (
                    "class_id",
                    models.CharField(
                        max_length=128,
                        validators=[
                            django.core.validators.RegexValidator(
                                message="scope identifier is invalid",
                                regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
                            )
                        ],
                    ),
                ),
                ("content_revision", models.PositiveBigIntegerField(default=0)),
                ("api_revision", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.RunPython(_backfill_class_scope, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="coursesource",
            name="class_id",
            field=models.CharField(
                default="class_1",
                max_length=128,
                validators=[
                    django.core.validators.RegexValidator(
                        message="scope identifier is invalid",
                        regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
                    )
                ],
            ),
        ),
        migrations.AlterField(
            model_name="knowledgeingestionjob",
            name="class_id",
            field=models.CharField(
                default="class_1",
                max_length=128,
                validators=[
                    django.core.validators.RegexValidator(
                        message="scope identifier is invalid",
                        regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
                    )
                ],
            ),
        ),
        migrations.AlterField(
            model_name="courseknowledgerelease",
            name="class_id",
            field=models.CharField(
                default="class_1",
                max_length=128,
                validators=[
                    django.core.validators.RegexValidator(
                        message="scope identifier is invalid",
                        regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
                    )
                ],
            ),
        ),
        migrations.RemoveIndex(
            model_name="coursesource",
            name="m0_source_course_state_idx",
        ),
        migrations.AddIndex(
            model_name="coursesource",
            index=models.Index(
                fields=["course_id", "class_id", "source_type", "status"],
                name="m0_source_course_state_idx",
            ),
        ),
        migrations.RemoveIndex(
            model_name="knowledgeingestionjob",
            name="m0_ingestion_course_idx",
        ),
        migrations.AddIndex(
            model_name="knowledgeingestionjob",
            index=models.Index(
                fields=["course_id", "class_id", "created_at"],
                name="m0_ingestion_course_idx",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="courseknowledgerelease",
            name="m0_release_course_version_unique",
        ),
        migrations.RemoveConstraint(
            model_name="courseknowledgerelease",
            name="m0_release_one_active_course",
        ),
        migrations.RemoveIndex(
            model_name="courseknowledgerelease",
            name="m0_release_course_state_idx",
        ),
        migrations.AddConstraint(
            model_name="courseknowledgerelease",
            constraint=models.UniqueConstraint(
                fields=("course_id", "class_id", "version_number"),
                name="m0_release_course_version_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="courseknowledgerelease",
            constraint=models.UniqueConstraint(
                condition=Q(status="active"),
                fields=("course_id", "class_id"),
                name="m0_release_one_active_course",
            ),
        ),
        migrations.AddIndex(
            model_name="courseknowledgerelease",
            index=models.Index(
                fields=["course_id", "class_id", "status", "version_number"],
                name="m0_release_course_state_idx",
            ),
        ),
        migrations.AddField(
            model_name="courseclassworkspace",
            name="active_release",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="active_workspace",
                to="m0_platform_web.courseknowledgerelease",
            ),
        ),
        migrations.AddConstraint(
            model_name="courseclassworkspace",
            constraint=models.UniqueConstraint(
                fields=("course_id", "class_id"),
                name="m0_workspace_scope_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="courseclassworkspace",
            index=models.Index(
                fields=["course_id", "class_id"],
                name="m0_workspace_scope_idx",
            ),
        ),
        migrations.RunPython(
            _set_active_release_pointers,
            migrations.RunPython.noop,
        ),
    ]
