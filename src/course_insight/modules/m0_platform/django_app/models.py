"""Pseudonymous Django identity and exact-scope M0 grants."""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


PSEUDONYMOUS_ACTOR_PATTERN = (
    r"^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$"
)
SCOPE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"

_actor_validator = RegexValidator(
    regex=PSEUDONYMOUS_ACTOR_PATTERN,
    message="actor_id must be pseudonymous",
    code="invalid_actor_id",
)
_scope_validator = RegexValidator(
    regex=SCOPE_ID_PATTERN,
    message="scope identifier is invalid",
    code="invalid_scope_id",
)
_checksum_validator = RegexValidator(
    regex=CHECKSUM_PATTERN,
    message="source checksum is invalid",
    code="invalid_source_checksum",
)


class RoleName(models.TextChoices):
    STUDENT = "student", "Student"
    TEACHER = "teacher", "Teacher"
    COURSE_ADMIN = "course_admin", "Course administrator"
    SYSTEM_ADMIN = "system_admin", "System administrator"


class User(AbstractUser):
    """Django account keyed to a non-identifying stable actor ID."""

    actor_id = models.CharField(
        max_length=128,
        unique=True,
        validators=[_actor_validator],
    )

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(actor_id__regex=PSEUDONYMOUS_ACTOR_PATTERN),
                name="m0_user_actor_id_pseudonymous",
            ),
            models.CheckConstraint(
                condition=(
                    Q(username=models.F("actor_id"))
                    & Q(email="")
                    & Q(first_name="")
                    & Q(last_name="")
                ),
                name="m0_user_identity_fields_safe",
            ),
        ]

    def clean(self) -> None:
        """Reject mutable aliases and real-world identity fields."""

        super().clean()
        errors: dict[str, str] = {}
        if self.username != self.actor_id:
            errors["username"] = "username must equal actor_id"
        for field_name in ("email", "first_name", "last_name"):
            if getattr(self, field_name):
                errors[field_name] = "real-world identity is not stored"
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        """Avoid rendering mutable or real-world identity fields."""

        return self.actor_id


class ActorGrant(models.Model):
    """One exact, revocable authorization relationship for a Web user."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="actor_grants",
    )
    role = models.CharField(max_length=32, choices=RoleName.choices)
    course_id = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        validators=[_scope_validator],
    )
    class_id = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        validators=[_scope_validator],
    )
    is_active = models.BooleanField(default=True)
    source_checksum = models.CharField(
        max_length=64,
        validators=[_checksum_validator],
    )
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
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
            ("view_course_files", "Can view published course files"),
            ("manage_course_roles", "Can manage course roles"),
            ("manage_platform", "Can manage the platform"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (
                        Q(role__in=(RoleName.STUDENT, RoleName.TEACHER))
                        & Q(course_id__isnull=False)
                        & Q(class_id__isnull=False)
                    )
                    | (
                        Q(role=RoleName.COURSE_ADMIN)
                        & Q(course_id__isnull=False)
                        & Q(class_id__isnull=True)
                    )
                    | (
                        Q(role=RoleName.SYSTEM_ADMIN)
                        & Q(course_id__isnull=True)
                        & Q(class_id__isnull=True)
                    )
                ),
                name="m0_grant_role_scope_shape",
            ),
            models.CheckConstraint(
                condition=(
                    Q(valid_until__isnull=True)
                    | Q(valid_until__gt=models.F("valid_from"))
                ),
                name="m0_grant_valid_interval",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_active=True, revoked_at__isnull=True)
                    | Q(is_active=False, revoked_at__isnull=False)
                ),
                name="m0_grant_active_revocation",
            ),
            models.CheckConstraint(
                condition=Q(source_checksum__regex=CHECKSUM_PATTERN),
                name="m0_grant_checksum_shape",
            ),
            models.UniqueConstraint(
                fields=("user", "role", "course_id", "class_id"),
                condition=Q(role__in=(RoleName.STUDENT, RoleName.TEACHER)),
                name="m0_grant_unique_class_scope",
            ),
            models.UniqueConstraint(
                fields=("user", "role", "course_id"),
                condition=Q(role=RoleName.COURSE_ADMIN),
                name="m0_grant_unique_course_scope",
            ),
            models.UniqueConstraint(
                fields=("user", "role"),
                condition=Q(role=RoleName.SYSTEM_ADMIN),
                name="m0_grant_unique_platform_scope",
            ),
        ]
        indexes = [
            models.Index(
                fields=(
                    "user",
                    "is_active",
                    "role",
                    "course_id",
                    "class_id",
                ),
                name="m0_grant_auth_scope_idx",
            ),
            models.Index(
                fields=("valid_until", "revoked_at"),
                name="m0_grant_validity_idx",
            ),
        ]

    def __str__(self) -> str:
        """Keep admin/debug rendering free of scope and identity details."""

        return f"ActorGrant<{self.role}>"


class RoleSyncState(models.Model):
    """Safe synchronization metadata for the canonical role seed."""

    source_name = models.CharField(
        primary_key=True,
        max_length=64,
        default="roles",
        editable=False,
    )
    source_checksum = models.CharField(
        max_length=64,
        validators=[_checksum_validator],
    )
    grant_count = models.PositiveIntegerField(default=0)
    synchronized_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(source_checksum__regex=CHECKSUM_PATTERN),
                name="m0_role_sync_checksum_shape",
            ),
        ]

    def __str__(self) -> str:
        """Return a fixed safe label rather than source paths."""

        return "RoleSyncState<roles>"


class LoginFailureBucket(models.Model):
    """Hashed login-failure state shared by all Django processes."""

    bucket_key = models.CharField(
        primary_key=True,
        max_length=64,
        validators=[_checksum_validator],
    )
    window_started_at = models.DateTimeField()
    failure_count = models.PositiveIntegerField()
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(bucket_key__regex=CHECKSUM_PATTERN),
                name="m0_login_bucket_key_shape",
            ),
            models.CheckConstraint(
                condition=Q(failure_count__gte=1),
                name="m0_login_bucket_positive_count",
            ),
            models.CheckConstraint(
                condition=(
                    Q(locked_until__isnull=True)
                    | Q(locked_until__gt=models.F("window_started_at"))
                ),
                name="m0_login_bucket_lock_interval",
            ),
        ]
        indexes = [
            models.Index(
                fields=("locked_until",),
                name="m0_login_bucket_lock_idx",
            ),
        ]

    def __str__(self) -> str:
        """Never expose the keyed login identity in diagnostics."""

        return "LoginFailureBucket<redacted>"


# Imported here so Django discovers the models while existing imports keep the
# stable ``django_app.models`` public surface.
from course_insight.modules.m0_platform.django_app.knowledge_models import (  # noqa: E402
    AssessmentProjectionReceipt,
    ClassConceptLearningSnapshot,
    ClassLearningSnapshot,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeChangeOperation,
    KnowledgeExtractionBatchCheckpoint,
    KnowledgeIngestionJob,
    LearnerConceptMastery,
    LearningProfileProjectionEvent,
    ReleaseConcept,
    ReleaseConceptSource,
    ReleaseQuestion,
    ReleaseQuestionConceptLink,
    ScopedDeepSeekConfiguration,
    SuggestedTeacherReviewCase,
    SuggestedTeacherReviewItem,
    TeacherItemReviewNote,
    WrongQuestionRecord,
)
