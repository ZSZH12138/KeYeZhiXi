"""Thread-safe desktop lifecycle state independent of Tkinter widgets."""

from __future__ import annotations

import logging
import threading
import webbrowser
from dataclasses import dataclass
from typing import Callable, Literal

from course_insight.desktop.bootstrap import (
    DesktopBootstrapError,
    configure_process_environment,
    create_initial_admin,
    initial_admin_required,
    migrate_database,
    prepare_installation,
)
from course_insight.desktop.paths import DesktopPaths
from course_insight.desktop.supervisor import (
    DesktopSupervisorError,
    ServiceSupervisor,
)


LOGIN_URL = "http://127.0.0.1:8000/accounts/login/"
_LOGGER = logging.getLogger(__name__)
Phase = Literal[
    "idle",
    "preparing",
    "admin_required",
    "starting",
    "running",
    "stopping",
    "stopped",
    "failed",
]


@dataclass(frozen=True, slots=True)
class DesktopStatus:
    phase: Phase
    message: str
    web: str
    ingestion: str
    outbox: str

    @classmethod
    def stopped(cls) -> "DesktopStatus":
        return cls(
            phase="stopped",
            message="平台未运行",
            web="已停止",
            ingestion="已停止",
            outbox="已停止",
        )


class DesktopController:
    """Coordinate bootstrap, services, browser, and immutable UI status."""

    def __init__(
        self,
        paths: DesktopPaths,
        supervisor: ServiceSupervisor,
        *,
        browser_open: Callable[[str], bool] = webbrowser.open,
    ) -> None:
        self.paths = paths
        self._supervisor = supervisor
        self._browser_open = browser_open
        self._operation_lock = threading.RLock()
        self._status_lock = threading.Lock()
        self._status = DesktopStatus(
            phase="idle",
            message="正在准备启动",
            web="未启动",
            ingestion="未启动",
            outbox="未启动",
        )
        self._initialized = False

    @property
    def status(self) -> DesktopStatus:
        with self._status_lock:
            return self._status

    def prepare_and_start(self) -> None:
        with self._operation_lock:
            self._set_all("preparing", "正在初始化本地平台", "准备中")
            try:
                prepare_installation(self.paths)
                configure_process_environment(self.paths)
                migrate_database()
                self._initialized = True
                if initial_admin_required():
                    self._set_all(
                        "admin_required",
                        "请创建首个管理员",
                        "等待管理员",
                    )
                    return
                self._start_services()
            except Exception as error:
                self._record_failure(error, stop_services=True)

    def create_admin_and_start(
        self,
        account_name: str,
        raw_password: str,
    ) -> None:
        with self._operation_lock:
            if self.status.phase != "admin_required":
                self._set_all("failed", "首次管理员入口已经关闭", "失败")
                return
            self._set_all("preparing", "正在创建首个管理员", "准备中")
            try:
                create_initial_admin(account_name, raw_password)
                self._start_services()
            except Exception as error:
                if isinstance(error, DesktopBootstrapError):
                    _log_failure("initial administrator setup failed", error)
                    self._set_all(
                        "admin_required",
                        str(error),
                        "等待管理员",
                    )
                    return
                self._record_failure(error, stop_services=True)

    def open_login_page(self) -> bool:
        try:
            opened = bool(self._browser_open(LOGIN_URL))
        except Exception as error:
            _log_failure("browser launch failed", error)
            opened = False
        if not opened and self.status.phase == "running":
            self._set_all(
                "running",
                "平台已运行，请点击“打开登录页”",
                "运行中",
            )
        return opened

    def restart(self) -> None:
        with self._operation_lock:
            try:
                self._supervisor.stop()
                if not self._initialized:
                    self.prepare_and_start()
                    return
                if initial_admin_required():
                    self._set_all(
                        "admin_required",
                        "请创建首个管理员",
                        "等待管理员",
                    )
                    return
                self._start_services()
            except Exception as error:
                self._record_failure(error, stop_services=True)

    def stop(self) -> None:
        with self._operation_lock:
            self._set_all("stopping", "正在停止平台", "停止中")
            self._supervisor.stop()
            self._set_status(DesktopStatus.stopped())

    def check_services(self) -> None:
        with self._operation_lock:
            if self.status.phase != "running":
                return
            failure = self._supervisor.poll_failure()
            if failure is None:
                return
            self._supervisor.stop()
            _LOGGER.error(
                "desktop service exited service=%s returncode=%s",
                failure.service,
                failure.returncode,
            )
            self._set_all(
                "failed",
                f"{failure.service} 服务异常退出（状态码 {failure.returncode}）",
                "失败",
            )

    def _start_services(self) -> None:
        self._set_all("starting", "正在启动平台和 Workers", "启动中")
        try:
            self._supervisor.start()
            self._supervisor.wait_ready(timeout_seconds=60.0)
        except Exception:
            self._supervisor.stop()
            raise
        opened = self.open_login_page()
        message = "平台已运行" if opened else "平台已运行，请点击“打开登录页”"
        self._set_all("running", message, "运行中")

    def _record_failure(
        self,
        error: Exception,
        *,
        stop_services: bool,
    ) -> None:
        _log_failure("desktop lifecycle failed", error)
        if stop_services:
            self._supervisor.stop()
        message = (
            str(error)
            if isinstance(error, (DesktopBootstrapError, DesktopSupervisorError))
            else "平台启动失败，请查看日志"
        )
        self._set_all("failed", message, "失败")

    def _set_all(self, phase: Phase, message: str, service: str) -> None:
        self._set_status(
            DesktopStatus(
                phase=phase,
                message=message,
                web=service,
                ingestion=service,
                outbox=service,
            )
        )

    def _set_status(self, status: DesktopStatus) -> None:
        with self._status_lock:
            self._status = status


def _log_failure(message: str, error: Exception) -> None:
    _LOGGER.error(
        "%s exception_type=%s",
        message,
        type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
