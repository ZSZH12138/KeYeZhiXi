"""Start, observe, and deterministically stop desktop service processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from course_insight.desktop.bootstrap import configure_process_environment
from course_insight.desktop.paths import DesktopPaths
from course_insight.desktop.services import (
    SERVICE_NAMES,
    build_service_command,
    is_frozen,
)


READY_URL = "http://127.0.0.1:8000/health/ready/"


class DesktopSupervisorError(RuntimeError):
    """Raised when the complete desktop service set cannot run safely."""


@dataclass(frozen=True, slots=True)
class ServiceFailure:
    service: str
    returncode: int


@dataclass(slots=True)
class _ServiceProcess:
    process: Any
    log: IO[str]


class ServiceSupervisor:
    """Own all child processes and their redirected output handles."""

    def __init__(
        self,
        paths: DesktopPaths,
        *,
        environment: Mapping[str, str] | None = None,
        frozen: bool | None = None,
        executable: Path | str | None = None,
        python_executable: Path | str | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        port_available: Callable[[], bool] | None = None,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        self._paths = paths
        self._environment = dict(
            os.environ if environment is None else environment
        )
        self._frozen = is_frozen() if frozen is None else frozen
        self._executable = Path(executable or sys.executable)
        self._python_executable = Path(python_executable or sys.executable)
        self._popen = popen_factory
        self._port_available = port_available or _port_8000_is_available
        self._stop_timeout_seconds = stop_timeout_seconds
        self._services: dict[str, _ServiceProcess] = {}

    @property
    def running_services(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in SERVICE_NAMES
            if name in self._services
            and self._services[name].process.poll() is None
        )

    def start(self) -> None:
        if self._services:
            raise DesktopSupervisorError("平台服务已经启动")
        if not self._port_available():
            raise DesktopSupervisorError("8000 端口已被占用")

        environment = configure_process_environment(
            self._paths,
            dict(self._environment),
        )
        try:
            for name in SERVICE_NAMES:
                self._start_service(name, environment)
        except Exception as error:
            self.stop()
            if isinstance(error, DesktopSupervisorError):
                raise
            raise DesktopSupervisorError("平台服务启动失败") from error

    def wait_ready(self, *, timeout_seconds: float = 60.0) -> None:
        wait_until_ready(
            READY_URL,
            timeout_seconds=timeout_seconds,
            failure_probe=self.poll_failure,
        )

    def poll_failure(self) -> ServiceFailure | None:
        for name in SERVICE_NAMES:
            record = self._services.get(name)
            if record is None:
                continue
            returncode = record.process.poll()
            if returncode is not None:
                return ServiceFailure(name, int(returncode))
        return None

    def stop(self) -> None:
        records = tuple(self._services.values())
        try:
            for record in records:
                if record.process.poll() is None:
                    record.process.terminate()
            for record in records:
                if record.process.poll() is not None:
                    continue
                try:
                    record.process.wait(timeout=self._stop_timeout_seconds)
                except subprocess.TimeoutExpired:
                    record.process.kill()
                    record.process.wait(timeout=self._stop_timeout_seconds)
        finally:
            for record in records:
                record.log.close()
            self._services.clear()

    def _start_service(
        self,
        name: str,
        environment: Mapping[str, str],
    ) -> None:
        command = build_service_command(
            name,
            frozen=self._frozen,
            executable=self._executable,
            python_executable=self._python_executable,
        )
        log_path = self._paths.logs_dir / f"{name}.log"
        log = log_path.open("a", encoding="utf-8", buffering=1)
        try:
            process = self._popen(
                command,
                cwd=str(self._paths.data_root),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            log.close()
            raise
        self._services[name] = _ServiceProcess(process=process, log=log)


def wait_until_ready(
    url: str,
    *,
    timeout_seconds: float,
    readiness_probe: Callable[[str], bool] | None = None,
    failure_probe: Callable[[], ServiceFailure | None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 0.2,
) -> None:
    """Wait for a real ready state while observing child-process failures."""

    probe = readiness_probe or _readiness_probe
    check_failure = failure_probe or (lambda: None)
    deadline = monotonic() + timeout_seconds
    while True:
        failure = check_failure()
        if failure is not None:
            raise DesktopSupervisorError(
                f"{failure.service} 服务异常退出（状态码 {failure.returncode}）"
            )
        if probe(url):
            return
        if monotonic() >= deadline:
            raise DesktopSupervisorError("平台启动超时")
        sleep(poll_interval_seconds)


def _readiness_probe(url: str) -> bool:
    try:
        with urlopen(url, timeout=1.0) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return desktop_capabilities_ready(payload)


def desktop_capabilities_ready(payload: object) -> bool:
    """Require login plus both Workers, while allowing zero initial courses."""

    if type(payload) is not dict:
        return False
    capabilities = payload.get("capabilities")
    if type(capabilities) is not dict:
        return False
    return all(
        capabilities.get(name) == "ready"
        for name in (
            "web_auth",
            "learning_outbox",
            "knowledge_ingestion",
        )
    )


def _port_8000_is_available() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 8000))
    except OSError:
        return False
    return True
