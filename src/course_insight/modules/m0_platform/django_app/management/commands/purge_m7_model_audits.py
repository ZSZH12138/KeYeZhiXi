"""Delete M7 model-call audits older than the approved retention window."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m7_local_model.service import M7_AUDIT_RETENTION_DAYS


class Command(BaseCommand):
    """Expose the fixed retention policy to a trusted scheduler."""

    help = "Purge privacy-minimized M7 model audits older than 180 days."

    def handle(self, *args: object, **options: object) -> None:
        del args, options
        try:
            container = runtime.get_application_container()
            deleted = container.m7_service.purge_expired_model_audits(
                now=timezone.now(),
                requester_role="system_admin",
                retention_days=M7_AUDIT_RETENTION_DAYS,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"M7 audit retention completed; deleted={deleted}"
                )
            )
        except CommandError:
            raise
        except Exception as error:
            raise CommandError(
                "M7 audit retention could not complete safely"
            ) from error
        finally:
            runtime.close_application_container()
