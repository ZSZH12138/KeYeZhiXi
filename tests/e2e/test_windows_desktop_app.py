from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pytest


DESKTOP_EXE = os.environ.get("KEYEZHIXI_DESKTOP_EXE", "")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not DESKTOP_EXE,
        reason="KEYEZHIXI_DESKTOP_EXE is required for packaged E2E",
    ),
]


def test_packaged_desktop_first_and_second_launch(tmp_path: Path) -> None:
    pytest.importorskip("pywin32_bootstrap")
    pywinauto = pytest.importorskip("pywinauto")
    psutil = pytest.importorskip("psutil")
    keyboard = pytest.importorskip("pywinauto.keyboard")

    executable = Path(DESKTOP_EXE).resolve()
    assert executable.is_file()
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(tmp_path / "local-app-data")

    first_process = subprocess.Popen(
        [str(executable)],
        cwd=executable.parent,
        env=environment,
    )
    try:
        desktop = pywinauto.Desktop(backend="uia")
        main_specification = desktop.window(
            title="课业智析",
            process=first_process.pid,
        )
        main_specification.wait("visible enabled ready", timeout=20)
        main_window = main_specification.wrapper_object()
        admin_dialog = main_specification.child_window(
            title="创建首个管理员",
            control_type="Window",
        )
        admin_dialog.wait("visible enabled ready", timeout=45)
        admin_dialog.set_focus()
        keyboard.send_keys(
            "desktopadmin{TAB}desktoppassword{TAB}"
            "desktoppassword{TAB}{TAB}{SPACE}",
            pause=0.03,
        )
        _wait_until(
            _login_page_ready,
            timeout_seconds=90,
        )
        with urlopen(
            "http://127.0.0.1:8000/accounts/login/",
            timeout=5,
        ) as response:
            assert response.status == 200

        first_service_pids = _service_process_ids(psutil, first_process.pid)
        assert len(first_service_pids) == 3
        main_window.close()
        first_process.wait(timeout=30)
        assert first_process.returncode == 0
        _wait_until(
            lambda: all(not psutil.pid_exists(pid) for pid in first_service_pids),
            timeout_seconds=15,
        )
    finally:
        _force_stop_process_tree(psutil, first_process)

    second_process = subprocess.Popen(
        [str(executable)],
        cwd=executable.parent,
        env=environment,
    )
    try:
        desktop = pywinauto.Desktop(backend="uia")
        main_specification = desktop.window(
            title="课业智析",
            process=second_process.pid,
        )
        main_specification.wait("visible enabled ready", timeout=20)
        main_window = main_specification.wrapper_object()
        _wait_until(
            _login_page_ready,
            timeout_seconds=90,
        )
        assert not any(
            control.element_info.control_type == "Window"
            and control.window_text() == "创建首个管理员"
            for control in main_window.descendants()
        )
        second_service_pids = _service_process_ids(psutil, second_process.pid)
        assert len(second_service_pids) == 3
        main_window.close()
        second_process.wait(timeout=30)
        assert second_process.returncode == 0
        _wait_until(
            lambda: all(not psutil.pid_exists(pid) for pid in second_service_pids),
            timeout_seconds=15,
        )
    finally:
        _force_stop_process_tree(psutil, second_process)


def _service_process_ids(psutil: object, parent_pid: int) -> tuple[int, ...]:
    process = psutil.Process(parent_pid)
    return tuple(
        child.pid
        for child in process.children(recursive=False)
        if "--service" in child.cmdline()
    )


def _force_stop_process_tree(psutil: object, process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
        root = None
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    if root is not None:
        try:
            root.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(children, timeout=5)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_until(
    condition: Callable[[], bool],
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError("condition did not become true before timeout")


def _login_page_ready() -> bool:
    try:
        with urlopen(
            "http://127.0.0.1:8000/accounts/login/",
            timeout=1,
        ) as response:
            return response.status == 200
    except (HTTPError, URLError, OSError):
        return False
