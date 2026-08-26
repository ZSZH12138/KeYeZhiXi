# Generated for the provenance-preserving knowledge-ingestion architecture.

import django.core.validators
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("m0_platform_web", "0003_configure_deepseek_permission"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="actorgrant",
            options={
                "permissions": [
                    ("start_assessment", "Can start an assessment"),
                    ("submit_assessment", "Can submit an assessment"),
                    ("view_own_result", "Can view own assessment result"),
                    ("view_own_feedback", "Can view own assessment feedback"),
                    ("view_class_analytics", "Can view class analytics"),
                    ("view_student_report", "Can view student reports"),
                    ("review_score", "Can review assessment scores"),
                    ("configure_deepseek", "Can configure the DeepSeek API"),
                    ("manage_course_knowledge", "Can manage course knowledge"),
                    ("ask_course_question", "Can ask cited course questions"),
                    ("manage_course_roles", "Can manage course roles"),
                    ("manage_platform", "Can manage the platform"),
                ]
            },
        ),
        migrations.CreateModel(
            name="CourseSource",
            fields=[
                ("source_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("course_id", models.CharField(max_length=128, validators=[django.core.validators.RegexValidator(message="scope identifier is invalid", regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")])),
                ("display_name", models.CharField(max_length=255)),
                ("source_type", models.CharField(choices=[("knowledge", "Knowledge"), ("question", "Question")], max_length=16)),
                ("status", models.CharField(choices=[("staged", "Staged"), ("active", "Active"), ("deleted", "Deleted")], default="staged", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="course_sources", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["course_id", "source_type", "status"], name="m0_source_course_state_idx")],
            },
        ),
        migrations.CreateModel(
            name="CourseSourceVersion",
            fields=[
                ("version_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version_number", models.PositiveIntegerField()),
                ("storage_key", models.CharField(max_length=69, unique=True, validators=[django.core.validators.RegexValidator(message="storage key must be opaque", regex="^[0-9a-f]{32}/[0-9a-f-]{36}$")])),
                ("sha256", models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(regex="^[0-9a-f]{64}$")])),
                ("media_type", models.CharField(max_length=128)),
                ("size_bytes", models.PositiveBigIntegerField()),
                ("status", models.CharField(choices=[("staged", "Staged"), ("active", "Active"), ("retired", "Retired"), ("failed", "Failed"), ("deleted", "Deleted")], default="staged", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="versions", to="m0_platform_web.coursesource")),
            ],
            options={
                "indexes": [models.Index(fields=["source", "status", "version_number"], name="m0_source_version_state_idx")],
                "constraints": [models.UniqueConstraint(fields=("source", "version_number"), name="m0_source_version_unique")],
            },
        ),
        migrations.CreateModel(
            name="KnowledgeIngestionJob",
            fields=[
                ("job_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("course_id", models.CharField(max_length=128, validators=[django.core.validators.RegexValidator(message="scope identifier is invalid", regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")])),
                ("change_set_checksum", models.CharField(max_length=64, unique=True, validators=[django.core.validators.RegexValidator(regex="^[0-9a-f]{64}$")])),
                ("status", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("succeeded", "Succeeded"), ("partially_succeeded", "Partially succeeded"), ("failed", "Failed")], default="queued", max_length=24)),
                ("progress", models.PositiveSmallIntegerField(default=0)),
                ("checkpoint", models.JSONField(default=dict)),
                ("error_code", models.CharField(blank=True, max_length=64, null=True)),
                ("worker_id", models.CharField(blank=True, max_length=128, null=True)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="knowledge_ingestion_jobs", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["status", "created_at"], name="m0_ingestion_queue_idx"),
                    models.Index(fields=["course_id", "created_at"], name="m0_ingestion_course_idx"),
                ],
                "constraints": [models.CheckConstraint(condition=models.Q(("progress__lte", 100)), name="m0_ingestion_progress_range")],
            },
        ),
        migrations.CreateModel(
            name="KnowledgeChangeOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sequence", models.PositiveIntegerField()),
                ("operation", models.CharField(choices=[("upsert_source", "Upsert source"), ("delete_source", "Delete source"), ("edit_question", "Edit question")], max_length=24)),
                ("payload", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("skipped", "Skipped")], default="pending", max_length=16)),
                ("error_code", models.CharField(blank=True, max_length=64, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations", to="m0_platform_web.knowledgeingestionjob")),
                ("source", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="change_operations", to="m0_platform_web.coursesource")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("job", "sequence"), name="m0_change_operation_order_unique")],
            },
        ),
        migrations.CreateModel(
            name="CourseKnowledgeRelease",
            fields=[
                ("release_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("course_id", models.CharField(max_length=128, validators=[django.core.validators.RegexValidator(message="scope identifier is invalid", regex="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")])),
                ("version_number", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("building", "Building"), ("active", "Active"), ("retired", "Retired"), ("failed", "Failed")], max_length=16)),
                ("content_checksum", models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(regex="^[0-9a-f]{64}$")])),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("job", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="release", to="m0_platform_web.knowledgeingestionjob")),
            ],
            options={
                "indexes": [models.Index(fields=["course_id", "status", "version_number"], name="m0_release_course_state_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("course_id", "version_number"), name="m0_release_course_version_unique"),
                    models.UniqueConstraint(condition=models.Q(("status", "active")), fields=("course_id",), name="m0_release_one_active_course"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReleaseConcept",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("concept_id", models.CharField(max_length=80)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField()),
                ("aliases", models.JSONField(default=list)),
                ("release", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="concepts", to="m0_platform_web.courseknowledgerelease")),
            ],
            options={
                "indexes": [models.Index(fields=["release", "name"], name="m0_release_concept_name_idx")],
                "constraints": [models.UniqueConstraint(fields=("release", "concept_id"), name="m0_release_concept_unique")],
            },
        ),
        migrations.CreateModel(
            name="ReleaseConceptSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("chunk_id", models.CharField(max_length=96)),
                ("locator", models.CharField(max_length=512)),
                ("span_start", models.PositiveIntegerField()),
                ("span_end", models.PositiveIntegerField()),
                ("relation_type", models.CharField(max_length=24)),
                ("concept", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="source_references", to="m0_platform_web.releaseconcept")),
                ("source_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="concept_references", to="m0_platform_web.coursesourceversion")),
            ],
            options={
                "indexes": [models.Index(fields=["source_version", "chunk_id"], name="m0_concept_source_chunk_idx")],
                "constraints": [
                    models.CheckConstraint(condition=models.Q(("span_end__gt", models.F("span_start"))), name="m0_concept_source_span_valid"),
                    models.UniqueConstraint(fields=("concept", "source_version", "chunk_id", "span_start", "span_end", "relation_type"), name="m0_concept_source_unique"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReleaseQuestion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("question_id", models.CharField(max_length=96)),
                ("question_type", models.CharField(max_length=24)),
                ("ordinal", models.PositiveIntegerField()),
                ("locator", models.CharField(max_length=512)),
                ("stem", models.TextField()),
                ("payload", models.JSONField(default=dict)),
                ("release", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="questions", to="m0_platform_web.courseknowledgerelease")),
                ("source_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="release_questions", to="m0_platform_web.coursesourceversion")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("release", "question_id"), name="m0_release_question_unique"),
                    models.UniqueConstraint(fields=("release", "source_version", "ordinal"), name="m0_release_question_order_unique"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ReleaseQuestionConceptLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("confidence", models.DecimalField(decimal_places=4, max_digits=5)),
                ("status", models.CharField(choices=[("usable", "Usable"), ("needs_review", "Needs review")], max_length=16)),
                ("evidence", models.JSONField(default=list)),
                ("concept", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="question_links", to="m0_platform_web.releaseconcept")),
                ("question", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="concept_links", to="m0_platform_web.releasequestion")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("question", "concept"), name="m0_question_concept_unique"),
                    models.CheckConstraint(condition=models.Q(("confidence__gte", 0), ("confidence__lte", 1)), name="m0_question_confidence_range"),
                ],
            },
        ),
    ]
