from __future__ import annotations

import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import pytest

from course_insight.desktop import __main__ as entrypoint
from course_insight.desktop.paths import DesktopPaths


def test_destroy_root_ignores_an_already_destroyed_tk_application() -> None:
    class DestroyedRoot:
        def winfo_exists(self) -> bool:
            raise tk.TclError("application has been destroyed")

        def destroy(self) -> None:
            pytest.fail("an already destroyed root was destroyed again")

    entrypoint._destroy_root(DestroyedRoot())


def test_destroy_root_closes_a_live_tk_application() -> None:
    calls: list[str] = []

    class LiveRoot:
        def winfo_exists(self) -> bool:
            return True

        def destroy(self) -> None:
            calls.append("destroy")

    entrypoint._destroy_root(LiveRoot())

    assert calls == ["destroy"]


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


def test_primary_launch_releases_the_owned_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class OwnedInstance:
        def acquire(self) -> bool:
            events.append("acquire")
            return True

        def release(self) -> None:
            events.append("release")

    monkeypatch.setattr(entrypoint, "SingleInstance", OwnedInstance)
    monkeypatch.setattr(
        entrypoint,
        "_run_primary_desktop",
        lambda: events.append("run") or 7,
    )

    assert entrypoint.run_desktop() == 7
    assert events == ["acquire", "run", "release"]


def test_primary_desktop_builds_runs_and_cleans_up_the_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from course_insight.desktop import ui

    events: list[str] = []
    data_root = tmp_path / "KeYeZhiXi"
    paths = DesktopPaths(
        data_root=data_root,
        config_dir=data_root / "config",
        runtime_dir=data_root / "runtime",
        logs_dir=data_root / "logs",
        resources_root=tmp_path / "bundle",
    )

    class FakeRoot:
        def winfo_exists(self) -> bool:
            return True

        def destroy(self) -> None:
            events.append("root:destroy")

    class FakeController:
        def stop(self) -> None:
            events.append("controller:stop")

    class FakeDesktopWindow:
        def __init__(self, root: object, controller: object) -> None:
            events.append("window:create")

        def run(self) -> None:
            events.append("window:run")

    fake_root = FakeRoot()
    fake_supervisor = object()
    fake_controller = FakeController()
    monkeypatch.setattr(tk, "Tk", lambda: fake_root)
    monkeypatch.setattr(entrypoint, "resolve_desktop_paths", lambda env: paths)
    monkeypatch.setattr(
        entrypoint,
        "prepare_installation",
        lambda selected: events.append("installation:prepare"),
    )
    monkeypatch.setattr(
        entrypoint,
        "configure_launcher_logging",
        lambda selected: events.append("logging:configure"),
    )
    monkeypatch.setattr(
        entrypoint,
        "ServiceSupervisor",
        lambda selected: fake_supervisor,
    )
    monkeypatch.setattr(
        entrypoint,
        "DesktopController",
        lambda selected, supervisor: fake_controller,
    )
    monkeypatch.setattr(ui, "DesktopWindow", FakeDesktopWindow)

    assert entrypoint._run_primary_desktop() == 0
    assert events == [
        "installation:prepare",
        "logging:configure",
        "window:create",
        "window:run",
        "controller:stop",
        "root:destroy",
    ]


def test_primary_desktop_reports_an_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeRoot:
        def withdraw(self) -> None:
            events.append(("root", "withdraw"))

        def winfo_exists(self) -> bool:
            return True

        def destroy(self) -> None:
            events.append(("root", "destroy"))

    fake_root = FakeRoot()
    monkeypatch.setattr(tk, "Tk", lambda: fake_root)
    monkeypatch.setattr(
        entrypoint,
        "resolve_desktop_paths",
        lambda env: (_ for _ in ()).throw(RuntimeError("invalid path")),
    )
    monkeypatch.setattr(
        messagebox,
        "showerror",
        lambda title, body, parent: events.append((title, parent)),
    )

    assert entrypoint._run_primary_desktop() == 1
    assert ("课业智析启动失败", fake_root) in events
    assert events[-1] == ("root", "destroy")
