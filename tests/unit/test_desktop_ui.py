from __future__ import annotations

import tkinter as tk
from pathlib import Path

from course_insight.desktop.controller import DesktopStatus
from course_insight.desktop.paths import DesktopPaths
from course_insight.desktop.ui import DesktopWindow, InitialAdminDialog


class FakeController:
    def __init__(self) -> None:
        self.status = DesktopStatus.stopped()
        self.calls: list[str] = []
        root = Path("C:/LocalAppData/KeYeZhiXi")
        self.paths = DesktopPaths(
            data_root=root,
            config_dir=root / "config",
            runtime_dir=root / "runtime",
            logs_dir=root / "logs",
            resources_root=Path("C:/app/_internal"),
        )

    def prepare_and_start(self) -> None:
        self.calls.append("start")

    def open_login_page(self) -> None:
        self.calls.append("open")

    def restart(self) -> None:
        self.calls.append("restart")

    def stop(self) -> None:
        self.calls.append("stop")

    def check_services(self) -> None:
        self.calls.append("check")


def test_status_window_exposes_named_controls_and_button_actions() -> None:
    root = tk.Tk()
    root.withdraw()
    controller = FakeController()
    window = DesktopWindow(
        root,
        controller,
        task_runner=lambda task: task(),
        schedule_polling=False,
    )
    try:
        assert root.title() == "课业智析"
        root.nametowidget(".actions.openLoginButton").invoke()
        root.nametowidget(".actions.restartButton").invoke()
        root.nametowidget(".actions.stopButton").invoke()
        assert controller.calls == ["open", "restart", "stop"]
    finally:
        window.destroy()


def test_initial_admin_dialog_masks_passwords_and_requires_confirmation() -> None:
    root = tk.Tk()
    root.withdraw()
    submitted: list[tuple[str, str]] = []
    dialog = InitialAdminDialog(
        root,
        on_submit=lambda account, password: submitted.append(
            (account, password)
        ),
        on_cancel=lambda: None,
    )
    try:
        account = dialog.window.nametowidget("form.accountNameEntry")
        password = dialog.window.nametowidget("form.passwordEntry")
        confirmation = dialog.window.nametowidget("form.confirmPasswordEntry")
        submit = dialog.window.nametowidget("form.submitButton")
        assert password.cget("show") != ""
        assert confirmation.cget("show") != ""

        account.insert(0, "administrator")
        password.insert(0, "secret-one")
        confirmation.insert(0, "secret-two")
        submit.invoke()
        assert submitted == []
        assert "两次输入的密码不一致" in dialog.error_text

        confirmation.delete(0, tk.END)
        confirmation.insert(0, "secret-one")
        submit.invoke()
        assert submitted == [("administrator", "secret-one")]
        assert password.get() == ""
        assert confirmation.get() == ""
    finally:
        if dialog.window.winfo_exists():
            dialog.window.destroy()
        root.destroy()
