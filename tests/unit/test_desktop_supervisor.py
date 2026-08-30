from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from course_insight.desktop.paths import DesktopPaths
from course_insight.desktop.supervisor import (
    DesktopSupervisorError,
    ServiceFailure,
    ServiceSupervisor,
    desktop_capabilities_ready,
    wait_until_ready,
)


class FakeProcess:
    def __init__(
        self,
        command: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        self.command = command
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_times: list[float] = []
        self.timeout_on_first_wait = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        self.wait_times.append(timeout)
        if self.timeout_on_first_wait and len(self.wait_times) == 1:
            raise subprocess.TimeoutExpired(self.command, timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def _paths(tmp_path: Path) -> DesktopPaths:
    root = tmp_path / "KeYeZhiXi"
    for directory in (root, root / "config", root / "runtime", root / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
    return DesktopPaths(
        data_root=root,
        config_dir=root / "config",
        runtime_dir=root / "runtime",
        logs_dir=root / "logs",
        resources_root=tmp_path / "bundle",
    )


def test_supervisor_starts_three_hidden_services_with_separate_logs(
    tmp_path: Path,
) -> None:
    processes: list[FakeProcess] = []

    def popen(command: tuple[str, ...], **kwargs: Any) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    supervisor = ServiceSupervisor(
        _paths(tmp_path),
        environment={"PATH": "safe", "DJANGO_SECRET_KEY": "not-forwarded"},
        frozen=True,
        executable=Path("C:/app/KeYeZhiXi.exe"),
        popen_factory=popen,
        port_available=lambda: True,
    )

    supervisor.start()

    assert [process.command[-1] for process in processes] == [
        "web",
        "ingestion",
        "outbox",
    ]
    assert supervisor.running_services == ("web", "ingestion", "outbox")
    assert len({process.kwargs["stdout"].name for process in processes}) == 3
    for process in processes:
        assert process.kwargs["shell"] is False
        assert process.kwargs["stdin"] is subprocess.DEVNULL
        assert process.kwargs["stderr"] is subprocess.STDOUT
        assert process.kwargs["cwd"] == str(tmp_path / "KeYeZhiXi")
        assert process.kwargs["env"]["PATH"] == "safe"
        assert "DJANGO_SECRET_KEY" not in process.kwargs["env"]

    supervisor.stop()


def test_supervisor_refuses_a_busy_port_before_starting_workers(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    supervisor = ServiceSupervisor(
        _paths(tmp_path),
        popen_factory=lambda command, **kwargs: calls.append(command),
        port_available=lambda: False,
    )

    with pytest.raises(DesktopSupervisorError, match="8000 端口已被占用"):
        supervisor.start()

    assert calls == []
    assert supervisor.running_services == ()


def test_supervisor_force_kills_a_service_that_does_not_stop(
    tmp_path: Path,
) -> None:
    processes: list[FakeProcess] = []

    def popen(command: tuple[str, ...], **kwargs: Any) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    supervisor = ServiceSupervisor(
        _paths(tmp_path),
        popen_factory=popen,
        port_available=lambda: True,
        stop_timeout_seconds=0.25,
    )
    supervisor.start()
    processes[1].timeout_on_first_wait = True

    supervisor.stop()

    assert all(process.terminated for process in processes)
    assert processes[1].killed is True
    assert supervisor.running_services == ()
    assert all(process.kwargs["stdout"].closed for process in processes)


def test_supervisor_reports_the_first_unexpected_service_exit(
    tmp_path: Path,
) -> None:
    processes: list[FakeProcess] = []

    def popen(command: tuple[str, ...], **kwargs: Any) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    supervisor = ServiceSupervisor(
        _paths(tmp_path),
        popen_factory=popen,
        port_available=lambda: True,
    )
    supervisor.start()
    processes[2].returncode = 7

    assert supervisor.poll_failure() == ServiceFailure(
        service="outbox",
        returncode=7,
    )
    supervisor.stop()


def test_readiness_waits_for_ready_instead_of_accepting_degraded() -> None:
    statuses = iter([False, False, True])
    clock_values = iter([0.0, 0.1, 0.2, 0.3])
    sleeps: list[float] = []

    wait_until_ready(
        "http://127.0.0.1:8000/health/ready/",
        timeout_seconds=1.0,
        readiness_probe=lambda _: next(statuses),
        failure_probe=lambda: None,
        monotonic=lambda: next(clock_values),
        sleep=sleeps.append,
        poll_interval_seconds=0.05,
    )

    assert sleeps == [0.05, 0.05]


def test_desktop_readiness_accepts_login_and_workers_without_a_course() -> None:
    payload = {
        "status": "degraded",
        "capabilities": {
            "web_auth": "ready",
            "course_runtime": "not_ready",
            "student_read": "not_ready",
            "learning_outbox": "ready",
            "knowledge_ingestion": "ready",
        },
    }

    assert desktop_capabilities_ready(payload) is True
    payload["capabilities"]["knowledge_ingestion"] = "not_ready"
    assert desktop_capabilities_ready(payload) is False


def test_readiness_stops_waiting_when_a_child_exits() -> None:
    with pytest.raises(
        DesktopSupervisorError,
        match="ingestion 服务异常退出",
    ):
        wait_until_ready(
            "http://127.0.0.1:8000/health/ready/",
            timeout_seconds=1.0,
            readiness_probe=lambda _: False,
            failure_probe=lambda: ServiceFailure("ingestion", 3),
            monotonic=lambda: 0.0,
            sleep=lambda _: None,
        )


def test_readiness_times_out_with_a_safe_message() -> None:
    clock_values = iter([0.0, 1.1])

    with pytest.raises(DesktopSupervisorError, match="平台启动超时"):
        wait_until_ready(
            "http://127.0.0.1:8000/health/ready/",
            timeout_seconds=1.0,
            readiness_probe=lambda _: False,
            failure_probe=lambda: None,
            monotonic=lambda: next(clock_values),
            sleep=lambda _: None,
        )
