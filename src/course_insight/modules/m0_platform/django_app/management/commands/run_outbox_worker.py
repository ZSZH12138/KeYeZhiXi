"""Run the durable M0 outbox worker as an independent process."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from course_insight.modules.m0_platform.django_app import runtime


class Command(BaseCommand):
    help = "Run the independent leased outbox worker."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--once",
            action="store_true",
            help="Perform one bounded claim/delivery cycle and exit.",
        )

    def handle(self, *args, **options) -> None:
        del args
        try:
            container = runtime.get_application_container(
                logging_filename=runtime.OUTBOX_WORKER_LOG_FILENAME,
            )
            worker = container.outbox_worker
            if worker is None:
                raise CommandError(
                    "outbox worker is unavailable for this configuration"
                )
            once = bool(options["once"])
            snapshot = worker.run(once=once)
            if once and getattr(snapshot, "last_error_code", None) is not None:
                raise CommandError(
                    "outbox worker could not start safely"
                )
            self.stdout.write(self.style.SUCCESS("outbox worker stopped"))
        except CommandError:
            raise
        except Exception as error:
            raise CommandError(
                "outbox worker could not start safely"
            ) from error
        finally:
            runtime.close_application_container()
