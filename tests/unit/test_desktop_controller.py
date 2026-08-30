from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.desktop import controller
from course_insight.desktop.controller import DesktopController
from course_insight.desktop.paths import DesktopPaths
from course_insight.desktop.supervisor import (
    DesktopSupervisorError,
    ServiceFailure,
)


class FakeSupervisor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.failure: ServiceFailure | None = None
        self.wait_error: Exception | None = None

    def start(self) -> None:
        self.events.append("services:start")

    def wait_ready(self, *, timeout_seconds: float = 60.0) -> None:
        self.events.append(f"services:ready:{timeout_seconds}")
        if self.wait_error is not None:
            raise self.wait_error

    def stop(self) -> None:
        self.events.append("services:stop")

    def poll_failure(self) -> ServiceFailure | None:
        return self.failure


def _paths(tmp_path: Path) -> DesktopPaths:
    root = tmp_path / "KeYeZhiXi"
    return DesktopPaths(
        data_root=root,
        config_dir=root / "config",
        runtime_dir=root / "runtime",
        logs_dir=root / "logs",
        resources_root=tmp_path / "bundle",
    )


def _dependencies(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    admin_required: bool,
) -> None:
    monkeypatch.setattr(
        controller,
        "prepare_installation",
        lambda paths: events.append("installation:prepare"),
    )
    monkeypatch.setattr(
        controller,
        "configure_process_environment",
        lambda paths: events.append("environment:configure"),
    )
    monkeypatch.setattr(
        controller,
        "migrate_database",
        lambda: events.append("database:migrate"),
    )
    monkeypatch.setattr(
        controller,
        "initial_admin_required",
        lambda: admin_required,
    )


def test_controller_opens_browser_only_after_every_service_is_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)
    supervisor = FakeSupervisor(events)
    desktop = DesktopController(
        _paths(tmp_path),
        supervisor,
        browser_open=lambda url: events.append(f"browser:{url}") or True,
    )

    desktop.prepare_and_start()

    assert desktop.status.phase == "running"
    assert events == [
        "installation:prepare",
        "environment:configure",
        "database:migrate",
        "services:start",
        "services:ready:60.0",
        "browser:http://127.0.0.1:8000/accounts/login/",
    ]


def test_controller_waits_for_local_admin_before_starting_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=True)
    monkeypatch.setattr(
        controller,
        "create_initial_admin",
        lambda account, password: events.append("administrator:created"),
    )
    desktop = DesktopController(
        _paths(tmp_path),
        FakeSupervisor(events),
        browser_open=lambda url: events.append(f"browser:{url}") or True,
    )

    desktop.prepare_and_start()
    assert desktop.status.phase == "admin_required"
    assert "services:start" not in events

    desktop.create_admin_and_start("local-admin", "local-password")

    assert desktop.status.phase == "running"
    assert events.index("administrator:created") < events.index("services:start")
    assert "local-password" not in repr(desktop.__dict__)


def test_controller_stops_all_services_when_readiness_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)
    supervisor = FakeSupervisor(events)
    supervisor.wait_error = DesktopSupervisorError("平台启动超时")
    desktop = DesktopController(
        _paths(tmp_path),
        supervisor,
        browser_open=lambda url: True,
    )

    desktop.prepare_and_start()

    assert desktop.status.phase == "failed"
    assert desktop.status.message == "平台启动超时"
    assert events[-1] == "services:stop"


def test_controller_keeps_running_when_the_browser_cannot_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)
    desktop = DesktopController(
        _paths(tmp_path),
        FakeSupervisor(events),
        browser_open=lambda url: False,
    )

    desktop.prepare_and_start()

    assert desktop.status.phase == "running"
    assert desktop.status.message == "平台已运行，请点击“打开登录页”"


def test_controller_keeps_running_when_browser_launch_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)

    def failed_browser_launch(url: str) -> bool:
        del url
        raise OSError("no browser association")

    desktop = DesktopController(
        _paths(tmp_path),
        FakeSupervisor(events),
        browser_open=failed_browser_launch,
    )

    desktop.prepare_and_start()

    assert desktop.status.phase == "running"
    assert desktop.status.message == "平台已运行，请点击“打开登录页”"
    assert "services:stop" not in events


def test_controller_detects_a_worker_exit_and_stops_the_complete_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)
    supervisor = FakeSupervisor(events)
    desktop = DesktopController(
        _paths(tmp_path),
        supervisor,
        browser_open=lambda url: True,
    )
    desktop.prepare_and_start()
    supervisor.failure = ServiceFailure("outbox", 9)

    desktop.check_services()

    assert desktop.status.phase == "failed"
    assert "outbox 服务异常退出" in desktop.status.message
    assert events[-1] == "services:stop"


def test_controller_restart_and_stop_have_deterministic_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)
    desktop = DesktopController(
        _paths(tmp_path),
        FakeSupervisor(events),
        browser_open=lambda url: True,
    )
    desktop.prepare_and_start()

    desktop.restart()
    assert desktop.status.phase == "running"
    desktop.stop()

    assert desktop.status.phase == "stopped"
    assert events.count("services:stop") == 2
    assert events.count("services:start") == 2


def test_controller_reports_a_restart_failure_instead_of_leaving_starting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _dependencies(monkeypatch, events, admin_required=False)
    supervisor = FakeSupervisor(events)
    desktop = DesktopController(
        _paths(tmp_path),
        supervisor,
        browser_open=lambda url: True,
    )
    desktop.prepare_and_start()
    supervisor.wait_error = DesktopSupervisorError("重新启动失败")

    desktop.restart()

    assert desktop.status.phase == "failed"
    assert desktop.status.message == "重新启动失败"
    assert events[-1] == "services:stop"
