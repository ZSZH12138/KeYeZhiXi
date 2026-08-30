"""Run retryable physical file cleanup after account deletion."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser

from course_insight.modules.m0_platform.django_app.account_file_cleanup import (
    purge_pending_erasure_files,
)


class Command(BaseCommand):
    help = "Physically delete the bounded queue of account-erasure files."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--limit", type=int, default=1_000)

    def handle(self, *args: object, **options: object) -> None:
        del args
        deleted = purge_pending_erasure_files(limit=int(options["limit"]))
        self.stdout.write(self.style.SUCCESS(f"deleted {deleted} erasure files"))
