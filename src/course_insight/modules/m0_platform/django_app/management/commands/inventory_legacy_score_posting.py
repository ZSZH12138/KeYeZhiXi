"""Dry-run inventory of completed submits that posted scores before review."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from course_insight.application.legacy_score_inventory import (
    inspect_legacy_score_posting,
)
from course_insight.infrastructure.json_io import write_json
from course_insight.modules.m0_platform.django_app import runtime


class Command(BaseCommand):
    """Scan payload-free M0 submit rows against M8 scoring and never call them clean."""

    help = (
        "Inventory completed submits that posted pending or rejected scores "
        "before teacher review. Dry-run is the default."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Attempt automatic rebuild (currently fail-closed).",
        )

    def handle(self, *args: object, **options: object) -> None:
        del args
        apply = bool(options.get("apply"))
        if apply:
            raise CommandError(
                "automatic M5 rewind is disabled; use dry-run inventory only"
            )
        try:
            container = runtime.get_application_container()
            flags = []
            list_runs = getattr(
                container.m0_service,
                "list_assessment_runs",
                None,
            )
            if not callable(list_runs):
                raise CommandError("M0 cannot list assessment runs for inventory")
            for run in list_runs():
                if run.operation != "submit" or run.attempt_id is None:
                    continue
                scoring = container.m8_service.get_scoring_result(run.attempt_id)
                if scoring is None:
                    continue
                flag = inspect_legacy_score_posting(run, scoring)
                if flag is None:
                    continue
                flags.append(flag.to_dict())
            report_path = (
                container.settings.runtime_dir / "legacy_score_inventory.json"
            )
            write_json(
                report_path,
                {
                    "flag": "legacy_score_posted_before_review",
                    "apply": apply,
                    "count": len(flags),
                    "flags": flags,
                },
            )
            self.stdout.write(
                self.style.WARNING(
                    f"legacy polluted submits={len(flags)}; none marked clean"
                )
            )
        except CommandError:
            raise
        except Exception as error:
            raise CommandError(
                "legacy score inventory could not complete safely"
            ) from error
        finally:
            runtime.close_application_container()
