"""Internal service modes shared by source and frozen desktop launches."""

from __future__ import annotations

import sys
from pathlib import Path

from django.core.management import call_command


SERVICE_NAMES = ("web", "ingestion", "outbox")


class DesktopServiceError(RuntimeError):
    """Raised for an invalid or unavailable internal desktop service."""


def build_service_command(
    name: str,
    *,
    frozen: bool,
    executable: Path | str,
    python_executable: Path | str,
) -> tuple[str, ...]:
    """Build a credential-free child command for one known service."""

    _require_service(name)
    if frozen:
        return (str(executable), "--service", name)
    return (
        str(python_executable),
        "-m",
        "course_insight.desktop",
        "--service",
        name,
    )


def run_service(name: str) -> None:
    """Run one existing Django management command in the current process."""

    _require_service(name)
    _ensure_django_ready()
    if name == "web":
        call_command(
            "runserver",
            "127.0.0.1:8000",
            use_reloader=False,
        )
        return
    if name == "ingestion":
        call_command("run_ingestion_worker")
        return
    call_command("run_outbox_worker")


def _require_service(name: str) -> None:
    if name not in SERVICE_NAMES:
        raise DesktopServiceError("未知的内部服务")


def _ensure_django_ready() -> None:
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))

