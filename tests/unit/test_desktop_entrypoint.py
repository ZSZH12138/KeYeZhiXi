from __future__ import annotations

import webbrowser

import pytest

from course_insight.desktop import __main__ as entrypoint


def test_internal_service_mode_does_not_create_the_desktop_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(entrypoint, "run_service", calls.append)
    monkeypatch.setattr(
        entrypoint,
        "run_desktop",
        lambda: pytest.fail("service mode opened the desktop window"),
    )

    assert entrypoint.main(["--service", "outbox"]) == 0
    assert calls == ["outbox"]


def test_diagnostics_mode_does_not_create_the_desktop_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        entrypoint,
        "run_diagnostics",
        lambda: calls.append("diagnostics") or 0,
    )
    monkeypatch.setattr(
        entrypoint,
        "run_desktop",
        lambda: pytest.fail("diagnostics mode opened the desktop window"),
    )

    assert entrypoint.main(["--diagnose"]) == 0
    assert calls == ["diagnostics"]


def test_duplicate_launch_opens_the_existing_login_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    class DuplicateInstance:
        def acquire(self) -> bool:
            return False

        def release(self) -> None:
            pytest.fail("duplicate launch released an unowned mutex")

    monkeypatch.setattr(entrypoint, "SingleInstance", DuplicateInstance)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        entrypoint,
        "_run_primary_desktop",
        lambda: pytest.fail("duplicate launch started another platform"),
    )

    assert entrypoint.run_desktop() == 0
    assert opened == ["http://127.0.0.1:8000/accounts/login/"]
