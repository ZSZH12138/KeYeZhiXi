"""Durable Django records for staged ingestion and atomic knowledge releases."""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from course_insight.modules.m0_platform.django_app.workspace_labels import (
    class_label,
    course_label,
)


_scope_validator = RegexValidator(
    regex=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    message="scope identifier is invalid",
)
_checksum_validator = RegexValidator(regex=r"^[0-9a-f]{64}$")
_storage_key_validator = RegexValidator(
    regex=r"^[0-9a-f]{32}/[0-9a-f-]{36}$",
    message="storage key must be opaque",
)


class CourseClassWorkspace(models.Model):
    """Current publication and configuration revisions for one class."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ARCHIVED = "archived", "Archived"

    workspace_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    course_id = models.CharField(max_length=128, validators=[_scope_validator])
    class_id = models.CharField(max_length=128, validators=[_scope_validator])
    course_display_name = models.CharField(max_length=255, blank=True, default="")
    class_display_name = models.CharField(max_length=255, blank=True, default="")
    owner_teacher = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_course_class_workspaces",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    roster_version = models.PositiveBigIntegerField(default=0)
    creation_token_digest = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
    )
    active_release = models.OneToOneField(
        "CourseKnowledgeRelease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_workspace",
    )
    content_revision = models.PositiveBigIntegerField(default=0)
    api_revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("course_id", "class_id"),
                name="m0_workspace_scope_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("course_id", "class_id"),
                name="m0_workspace_scope_idx",
            )
        ]

    def is_owned_by(self, user: object) -> bool:
        """Return whether ``user`` is the active owner of this workspace."""

        return (
            self.status == self.Status.ACTIVE
            and self.owner_teacher_id is not None
            and self.owner_teacher_id == getattr(user, "pk", None)
        )

    @property
    def course_display_label(self) -> str:
        """Return the course name safe for teacher/student presentation."""

        return course_label(self.course_id, self.course_display_name)

    @property
    def class_display_label(self) -> str:
        """Return the class name safe for teacher/student presentation."""

        return class_label(self.class_id, self.class_display_name)


class ScopedDeepSeekConfiguration(models.Model):
    """Encrypted teacher DeepSeek settings for one exact workspace."""

    workspace = models.OneToOneField(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="deepseek_configuration",
    )
    encrypted_api_key = models.BinaryField(null=True, blank=True)
    encryption_scheme = models.CharField(max_length=32, blank=True, default="")
    masked_key = models.CharField(max_length=32, blank=True, default="")
    model_name = models.CharField(max_length=64, default="deepseek-v4-flash")
    thinking_enabled = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scoped_deepseek_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)


class LearnerConceptMastery(models.Model):
    """Authoritative count-based mastery for one learner and exact class."""

    class AttemptStatus(models.TextChoices):
        UNSEEN = "unseen", "Unseen"
        ATTEMPTED = "attempted", "Attempted"

    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="learner_masteries",
    )
    learner = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="concept_masteries",
    )
    concept_id = models.CharField(max_length=80)
    attempted_count = models.PositiveBigIntegerField(default=0)
    correct_count = models.PositiveBigIntegerField(default=0)
    attempt_status = models.CharField(
        max_length=16,
        choices=AttemptStatus.choices,
        default=AttemptStatus.UNSEEN,
    )
    mastery = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=0,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "learner", "concept_id"),
                name="m0_mastery_scope_learner_concept_unique",
            ),
            models.CheckConstraint(
                condition=Q(correct_count__lte=models.F("attempted_count")),
                name="m0_mastery_correct_lte_attempted",
            ),
            models.CheckConstraint(
                condition=Q(mastery__gte=0) & Q(mastery__lte=0.9),
                name="m0_mastery_capped_range",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        attempt_status="unseen",
                        attempted_count=0,
                        correct_count=0,
                        mastery=0,
                    )
                    | Q(
                        attempt_status="attempted",
                        attempted_count__gt=0,
                    )
                ),
                name="m0_mastery_attempt_status_consistent",
            ),
        ]
        indexes = [
            models.Index(
                fields=("workspace", "learner", "mastery"),
                name="m0_mastery_selection_idx",
            )
        ]


class WrongQuestionRecord(models.Model):
    """Current correction ledger independent from mastery calculations."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        RETIRED = "retired", "Retired"

    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="wrong_questions",
    )
    learner = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="wrong_questions",
    )
    item_id = models.CharField(max_length=128)
    item_version = models.CharField(max_length=128)
    latest_attempt_id = models.CharField(max_length=128)
    source_paper_id = models.CharField(max_length=128, blank=True, default="")
    source_item_instance_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    resolved_through_attempt_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    active_follow_up_paper_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    active_follow_up_item_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
    )
    hint_revealed = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    wrong_count = models.PositiveBigIntegerField(default=1)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "learner", "item_id"),
                name="m0_wrong_scope_learner_item_unique",
            ),
            models.CheckConstraint(
                condition=Q(wrong_count__gte=1),
                name="m0_wrong_positive_count",
            ),
        ]
        indexes = [
            models.Index(
                fields=("workspace", "learner", "status"),
                name="m0_wrong_selection_idx",
            ),
            models.Index(
                fields=("workspace", "active_follow_up_paper_id"),
                name="m0_wrong_followup_idx",
            ),
        ]


class AssessmentProjectionReceipt(models.Model):
    """Idempotency receipt for projecting one finalized scoring result."""

    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="assessment_projection_receipts",
    )
    learner = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="assessment_projection_receipts",
    )
    attempt_id = models.CharField(max_length=128, unique=True)
    paper_id = models.CharField(max_length=128)
    task_type = models.CharField(max_length=32)
    scoring_checksum = models.CharField(max_length=64)
    projection_payload = models.JSONField(default=dict)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(task_type__in=("diagnostic", "stage_assessment")),
                name="m0_projection_profile_task_only",
            )
        ]


class ClassLearningSnapshot(models.Model):
    """Immutable M5 class-level projection of the currently active roster."""

    snapshot_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="class_learning_snapshots",
    )
    release = models.ForeignKey(
        "CourseKnowledgeRelease",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="class_learning_snapshots",
    )
    input_checksum = models.CharField(max_length=64, validators=[_checksum_validator])
    roster_checksum = models.CharField(max_length=64, validators=[_checksum_validator])
    active_student_count = models.PositiveIntegerField(default=0)
    reason = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "input_checksum"),
                name="m5_class_snapshot_input_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("workspace", "created_at"),
                name="m5_class_snapshot_latest_idx",
            )
        ]


class ClassConceptLearningSnapshot(models.Model):
    """One named knowledge-point aggregate in an immutable class snapshot."""

    snapshot = models.ForeignKey(
        ClassLearningSnapshot,
        on_delete=models.CASCADE,
        related_name="concepts",
    )
    concept_id = models.CharField(max_length=80)
    concept_name = models.CharField(max_length=255)
    attempted_student_count = models.PositiveIntegerField(default=0)
    unattempted_student_count = models.PositiveIntegerField(default=0)
    attempted_item_count = models.PositiveBigIntegerField(default=0)
    correct_item_count = models.PositiveBigIntegerField(default=0)
    average_mastery = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    priority_support_count = models.PositiveIntegerField(default=0)
    priority_support_rate = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default=0,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("snapshot", "concept_id"),
                name="m5_class_snapshot_concept_unique",
            ),
            models.CheckConstraint(
                condition=Q(correct_item_count__lte=models.F("attempted_item_count")),
                name="m5_class_snapshot_correct_lte_attempted",
            ),
            models.CheckConstraint(
                condition=Q(average_mastery__gte=0) & Q(average_mastery__lte=0.9),
                name="m5_class_snapshot_mastery_range",
            ),
            models.CheckConstraint(
                condition=(
                    Q(priority_support_rate__gte=0)
                    & Q(priority_support_rate__lte=1)
                ),
                name="m5_class_snapshot_support_rate_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=("snapshot", "concept_id"),
                name="m5_class_snapshot_concept_idx",
            )
        ]


class LearningProfileProjectionEvent(models.Model):
    """Append-only record linking a profile change to the M5/M9 refresh path."""

    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="learning_profile_projection_events",
    )
    learner = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="learning_profile_projection_events",
    )
    attempt_id = models.CharField(max_length=128)
    task_type = models.CharField(max_length=32)
    previous_scoring_checksum = models.CharField(
        max_length=64,
        validators=[_checksum_validator],
        null=True,
        blank=True,
    )
    scoring_checksum = models.CharField(max_length=64, validators=[_checksum_validator])
    changed_concept_ids = models.JSONField(default=list)
    reason = models.CharField(max_length=32)
    snapshot = models.ForeignKey(
        ClassLearningSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="projection_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "attempt_id", "scoring_checksum"),
                name="m5_profile_event_idempotency_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("workspace", "created_at"),
                name="m5_profile_event_latest_idx",
            ),
            models.Index(
                fields=("learner", "attempt_id"),
                name="m5_profile_event_learner_idx",
            ),
        ]


class TeacherItemReviewNote(models.Model):
    """Teacher-visible note attached to one immutable item review version."""

    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="teacher_item_review_notes",
    )
    learner = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="teacher_item_review_notes",
    )
    reviewed_by = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authored_item_review_notes",
    )
    attempt_id = models.CharField(max_length=128)
    paper_id = models.CharField(max_length=128)
    item_instance_id = models.CharField(max_length=128)
    audit_id = models.CharField(max_length=128)
    audit_version = models.PositiveIntegerField()
    score = models.DecimalField(max_digits=9, decimal_places=3)
    max_score = models.DecimalField(max_digits=9, decimal_places=3)
    teacher_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("attempt_id", "item_instance_id", "audit_version"),
                name="m0_item_review_note_version_unique",
            ),
            models.CheckConstraint(
                condition=Q(score__gte=0) & Q(score__lte=models.F("max_score")),
                name="m0_item_review_note_score_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=("workspace", "learner", "attempt_id"),
                name="m0_item_review_note_lookup_idx",
            ),
            models.Index(
                fields=("attempt_id", "item_instance_id", "audit_version"),
                name="m0_item_note_version_idx",
            ),
        ]


class SuggestedTeacherReviewCase(models.Model):
    """One grouped low-confidence review case for one attempt and paper."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        SUPERSEDED = "superseded", "Superseded"

    workspace = models.ForeignKey(
        CourseClassWorkspace,
        on_delete=models.CASCADE,
        related_name="suggested_teacher_review_cases",
    )
    learner = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="suggested_teacher_review_cases",
    )
    attempt_id = models.CharField(max_length=128)
    paper_id = models.CharField(max_length=128)
    task_type = models.CharField(max_length=32)
    scoring_checksum = models.CharField(max_length=64, validators=[_checksum_validator])
    attempted_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "attempt_id", "paper_id"),
                name="m0_suggested_review_case_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("workspace", "status", "attempted_at"),
                name="m0_suggested_case_queue_idx",
            )
        ]


class SuggestedTeacherReviewItem(models.Model):
    """One current low-confidence item contained by a grouped review case."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        SUPERSEDED = "superseded", "Superseded"

    case = models.ForeignKey(
        SuggestedTeacherReviewCase,
        on_delete=models.CASCADE,
        related_name="items",
    )
    item_instance_id = models.CharField(max_length=128)
    audit_id = models.CharField(max_length=128)
    audit_version = models.PositiveIntegerField()
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    review_reasons = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("case", "item_instance_id", "audit_version"),
                name="m0_suggested_review_item_unique",
            ),
            models.CheckConstraint(
                condition=Q(confidence__gte=0) & Q(confidence__lte=1),
                name="m0_suggested_item_conf_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=("case", "status", "item_instance_id"),
                name="m0_suggested_item_queue_idx",
            )
        ]


class CourseSource(models.Model):
    """Logical teacher-managed file, independent of uploaded versions."""

    class SourceType(models.TextChoices):
        KNOWLEDGE = "knowledge", "Knowledge"
        QUESTION = "question", "Question"

    class Status(models.TextChoices):
        STAGED = "staged", "Staged"
        ACTIVE = "active", "Active"
        PENDING_DELETE = "pending_delete", "Pending deletion"
        DELETED = "deleted", "Deleted"

    source_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_id = models.CharField(max_length=128, validators=[_scope_validator])
    class_id = models.CharField(
        max_length=128,
        validators=[_scope_validator],
        default="class_1",
    )
    display_name = models.CharField(max_length=255)
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STAGED)
    created_by = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="course_sources",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("course_id", "class_id", "source_type", "status"),
                name="m0_source_course_state_idx",
            )
        ]


class CourseSourceVersion(models.Model):
    """Immutable uploaded bytes addressed only by an opaque storage key."""

    class Status(models.TextChoices):
        STAGED = "staged", "Staged"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"
        FAILED = "failed", "Failed"
        DELETED = "deleted", "Deleted"

    version_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(
        CourseSource,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    storage_key = models.CharField(
        max_length=69,
        unique=True,
        validators=[_storage_key_validator],
    )
    sha256 = models.CharField(max_length=64, validators=[_checksum_validator])
    media_type = models.CharField(max_length=128)
    size_bytes = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.STAGED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source", "version_number"),
                name="m0_source_version_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("source", "status", "version_number"),
                name="m0_source_version_state_idx",
            )
        ]


class KnowledgeIngestionJob(models.Model):
    """Restartable background build that never changes the active release early."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partially_succeeded", "Partially succeeded"
        FAILED = "failed", "Failed"

    _TRANSITIONS = {
        Status.QUEUED: frozenset({Status.RUNNING, Status.FAILED}),
        Status.RUNNING: frozenset({Status.SUCCEEDED, Status.PARTIAL, Status.FAILED}),
        Status.SUCCEEDED: frozenset(),
        Status.PARTIAL: frozenset(),
        Status.FAILED: frozenset(),
    }

    job_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_id = models.CharField(max_length=128, validators=[_scope_validator])
    class_id = models.CharField(
        max_length=128,
        validators=[_scope_validator],
        default="class_1",
    )
    requested_by = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="knowledge_ingestion_jobs",
    )
    change_set_checksum = models.CharField(
        max_length=64,
        db_index=True,
        validators=[_checksum_validator],
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    progress = models.PositiveSmallIntegerField(default=0)
    checkpoint = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    worker_id = models.CharField(max_length=128, null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(progress__lte=100),
                name="m0_ingestion_progress_range",
            )
        ]
        indexes = [
            models.Index(
                fields=("status", "created_at"),
                name="m0_ingestion_queue_idx",
            ),
            models.Index(
                fields=("course_id", "class_id", "created_at"),
                name="m0_ingestion_course_idx",
            ),
        ]

    def transition_to(self, target: str, *, error_code: str | None = None) -> None:
        """Persist one valid state transition with consistent timestamps."""

        if target not in self._TRANSITIONS.get(self.status, frozenset()):
            raise ValidationError("knowledge ingestion job transition is invalid")
        self.status = target
        now = timezone.now()
        update_fields = ["status", "updated_at"]
        if target == self.Status.RUNNING:
            self.started_at = self.started_at or now
            self.progress = max(self.progress, 1)
            update_fields.extend(("started_at", "progress"))
        if target in {self.Status.SUCCEEDED, self.Status.PARTIAL, self.Status.FAILED}:
            self.completed_at = now
            if target in {self.Status.SUCCEEDED, self.Status.PARTIAL}:
                self.progress = 100
            self.error_code = error_code
            update_fields.extend(("completed_at", "progress", "error_code"))
        self.save(update_fields=tuple(update_fields))


class KnowledgeExtractionBatchCheckpoint(models.Model):
    """Durable result for one deterministic knowledge-extraction batch."""

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    checkpoint_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    job = models.ForeignKey(
        KnowledgeIngestionJob,
        on_delete=models.CASCADE,
        related_name="extraction_batch_checkpoints",
    )
    batch_id = models.CharField(max_length=128)
    batch_index = models.PositiveIntegerField()
    character_count = models.PositiveIntegerField()
    execution_checksum = models.CharField(
        max_length=64,
        validators=[_checksum_validator],
    )
    status = models.CharField(max_length=16, choices=Status.choices)
    candidates = models.JSONField(default=list)
    evidence_chunks = models.JSONField(default=list)
    retry_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    exception_type = models.CharField(max_length=128, null=True, blank=True)
    completed_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("job", "batch_id"),
                name="m0_extract_checkpoint_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=("job", "status", "batch_index"),
                name="m0_extract_checkpoint_idx",
            )
        ]


class KnowledgeChangeOperation(models.Model):
    """One ordered file addition, deletion or question edit in a confirmed job."""

    class Operation(models.TextChoices):
        UPSERT = "upsert_source", "Upsert source"
        DELETE = "delete_source", "Delete source"
        EDIT_QUESTION = "edit_question", "Edit question"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    job = models.ForeignKey(
        KnowledgeIngestionJob,
        on_delete=models.CASCADE,
        related_name="operations",
    )
    sequence = models.PositiveIntegerField()
    operation = models.CharField(max_length=24, choices=Operation.choices)
    source = models.ForeignKey(
        CourseSource,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="change_operations",
    )
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_code = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("job", "sequence"),
                name="m0_change_operation_order_unique",
            )
        ]


class CourseKnowledgeRelease(models.Model):
    """Immutable build whose active row is the course publication pointer."""

    class Status(models.TextChoices):
        BUILDING = "building", "Building"
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"
        FAILED = "failed", "Failed"

    release_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course_id = models.CharField(max_length=128, validators=[_scope_validator])
    class_id = models.CharField(
        max_length=128,
        validators=[_scope_validator],
        default="class_1",
    )
    version_number = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices)
    job = models.OneToOneField(
        KnowledgeIngestionJob,
        on_delete=models.PROTECT,
        related_name="release",
    )
    content_checksum = models.CharField(max_length=64, validators=[_checksum_validator])
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("course_id", "class_id", "version_number"),
                name="m0_release_course_version_unique",
            ),
            models.UniqueConstraint(
                fields=("course_id", "class_id"),
                condition=Q(status="active"),
                name="m0_release_one_active_course",
            ),
        ]
        indexes = [
            models.Index(
                fields=("course_id", "class_id", "status", "version_number"),
                name="m0_release_course_state_idx",
            )
        ]


class ReleaseConcept(models.Model):
    """One canonical concept frozen inside a release."""

    release = models.ForeignKey(
        CourseKnowledgeRelease,
        on_delete=models.CASCADE,
        related_name="concepts",
    )
    concept_id = models.CharField(max_length=80)
    name = models.CharField(max_length=255)
    description = models.TextField()
    aliases = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("release", "concept_id"),
                name="m0_release_concept_unique",
            )
        ]
        indexes = [
            models.Index(fields=("release", "name"), name="m0_release_concept_name_idx")
        ]


class ReleaseConceptSource(models.Model):
    """Lossless source index for one concept evidence span."""

    concept = models.ForeignKey(
        ReleaseConcept,
        on_delete=models.CASCADE,
        related_name="source_references",
    )
    source_version = models.ForeignKey(
        CourseSourceVersion,
        on_delete=models.PROTECT,
        related_name="concept_references",
    )
    chunk_id = models.CharField(max_length=96)
    locator = models.CharField(max_length=512)
    chunk_text = models.TextField()
    span_start = models.PositiveIntegerField()
    span_end = models.PositiveIntegerField()
    relation_type = models.CharField(max_length=24)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(span_end__gt=models.F("span_start")),
                name="m0_concept_source_span_valid",
            ),
            models.UniqueConstraint(
                fields=(
                    "concept",
                    "source_version",
                    "chunk_id",
                    "span_start",
                    "span_end",
                    "relation_type",
                ),
                name="m0_concept_source_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=("source_version", "chunk_id"),
                name="m0_concept_source_chunk_idx",
            )
        ]


class ReleaseQuestion(models.Model):
    """One parsed question frozen inside a release."""

    release = models.ForeignKey(
        CourseKnowledgeRelease,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    question_id = models.CharField(max_length=96)
    source_version = models.ForeignKey(
        CourseSourceVersion,
        on_delete=models.PROTECT,
        related_name="release_questions",
    )
    question_type = models.CharField(max_length=24)
    ordinal = models.PositiveIntegerField()
    locator = models.CharField(max_length=512)
    stem = models.TextField()
    payload = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("release", "question_id"),
                name="m0_release_question_unique",
            ),
            models.UniqueConstraint(
                fields=("release", "source_version", "ordinal"),
                name="m0_release_question_order_unique",
            ),
        ]


class ReleaseQuestionConceptLink(models.Model):
    """Automatic multi-label question-to-concept relation for one release."""

    class Status(models.TextChoices):
        USABLE = "usable", "Usable"
        NEEDS_REVIEW = "needs_review", "Needs review"

    question = models.ForeignKey(
        ReleaseQuestion,
        on_delete=models.CASCADE,
        related_name="concept_links",
    )
    concept = models.ForeignKey(
        ReleaseConcept,
        on_delete=models.CASCADE,
        related_name="question_links",
    )
    confidence = models.DecimalField(max_digits=5, decimal_places=4)
    status = models.CharField(max_length=16, choices=Status.choices)
    evidence = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("question", "concept"),
                name="m0_question_concept_unique",
            ),
            models.CheckConstraint(
                condition=Q(confidence__gte=0) & Q(confidence__lte=1),
                name="m0_question_confidence_range",
            ),
        ]
