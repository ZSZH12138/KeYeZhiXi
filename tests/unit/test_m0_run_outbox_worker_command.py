from __future__ import annotations

import os
from types import SimpleNamespace

import django
import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402


def test_run_outbox_worker_command_uses_application_container_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, bool | None]] = []

    class _Worker:
        def run(self, *, once: bool = False) -> None:
            calls.append(("run", once))

    def _get_application_container() -> object:
        calls.append(("container", None))
        return SimpleNamespace(outbox_worker=_Worker())

    def _close_application_container() -> None:
        calls.append(("close", None))

    def _unexpected_web_runtime() -> object:
        raise AssertionError("Web runtime manifest should not be required")

    monkeypatch.setattr(
        runtime,
        "get_application_container",
        _get_application_container,
    )
    monkeypatch.setattr(
        runtime,
        "close_application_container",
        _close_application_container,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", _unexpected_web_runtime)

    call_command("run_outbox_worker", once=True)

    assert calls == [
        ("container", None),
        ("run", True),
        ("close", None),
    ]


def test_run_outbox_worker_once_fails_when_worker_reports_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Worker:
        def run(self, *, once: bool = False):
            calls.append(f"run:{once}")
            return SimpleNamespace(last_error_code="OUTBOX_DATABASE_UNAVAILABLE")

    monkeypatch.setattr(
        runtime,
        "get_application_container",
        lambda: SimpleNamespace(outbox_worker=_Worker()),
    )
    monkeypatch.setattr(
        runtime,
        "close_application_container",
        lambda: calls.append("close"),
    )

    with pytest.raises(CommandError, match="outbox worker could not start safely"):
        call_command("run_outbox_worker", once=True)

    assert calls == ["run:True", "close"]
