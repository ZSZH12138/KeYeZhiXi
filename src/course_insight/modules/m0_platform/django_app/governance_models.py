"""Durable account, class-roster, and deletion-governance records."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone


class ClassMembership(models.Model):
    """A student's current or historical membership in a teacher-owned class."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REMOVED = "removed", "Removed"

    workspace = models.ForeignKey(
        "m0_platform_web.CourseClassWorkspace",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    student = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="class_memberships",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    added_by_teacher = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="class_memberships_added",
    )
    removed_by_teacher = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="class_memberships_removed",
    )
    joined_at = models.DateTimeField(default=timezone.now)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("workspace", "student"),
                condition=Q(status="active"),
                name="m0_membership_unique_active_student",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="active", removed_at__isnull=True)
                    | Q(status="removed", removed_at__isnull=False)
                ),
                name="m0_membership_status_removed_at",
            ),
        ]
        indexes = [
            models.Index(
                fields=("workspace", "status", "student"),
                name="m0_membership_roster_idx",
            ),
            models.Index(
                fields=("student", "status", "workspace"),
                name="m0_membership_student_idx",
            ),
        ]


class AccountLifecycleEvent(models.Model):
    """Identity-minimised audit evidence for administrator account actions."""

    class Action(models.TextChoices):
        CREATED = "created", "Created"
        DELETION_STARTED = "deletion_started", "Deletion started"
        DELETED = "deleted", "Deleted"
        DELETE_FAILED = "delete_failed", "Deletion failed"

    action = models.CharField(max_length=32, choices=Action.choices)
    target_digest = models.CharField(max_length=64, db_index=True)
    target_account_type = models.CharField(max_length=16)
    administrator = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="account_lifecycle_events",
    )
    request_id = models.CharField(max_length=64, blank=True, default="")
    success = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("created_at", "action"),
                name="m0_account_event_time_idx",
            )
        ]


class DeletedActorFingerprint(models.Model):
    """A one-way marker preventing accidental reuse after physical deletion."""

    target_digest = models.CharField(primary_key=True, max_length=64)
    deleted_at = models.DateTimeField(default=timezone.now)


class AuthenticatedSession(models.Model):
    """Maps active Django sessions to users so deletion can revoke them."""

    session_key = models.CharField(primary_key=True, max_length=40)
    user = models.ForeignKey(
        "m0_platform_web.User",
        on_delete=models.CASCADE,
        related_name="authenticated_sessions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
