"""Small Tkinter status and first-administrator windows."""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from course_insight.desktop.controller import DesktopController


TaskRunner = Callable[[Callable[[], None]], None]


class DesktopWindow:
    """Render status while delegating all lifecycle work to the controller."""

    def __init__(
        self,
        root: tk.Tk,
        controller: DesktopController,
        *,
        task_runner: TaskRunner | None = None,
        schedule_polling: bool = True,
    ) -> None:
        self.root = root
        self._controller = controller
        self._task_runner = task_runner or _threaded_task_runner
        self._poll_id: str | None = None
        self._admin_dialog: InitialAdminDialog | None = None
        self._close_when_stopped = False
        self._message = tk.StringVar(value="正在准备启动")
        self._web = tk.StringVar(value="未启动")
        self._ingestion = tk.StringVar(value="未启动")
        self._outbox = tk.StringVar(value="未启动")
        self._build()
        if schedule_polling:
            self._poll_id = self.root.after(150, self._poll)

    def run(self) -> None:
        self._task_runner(self._controller.prepare_and_start)
        self.root.mainloop()

    def destroy(self) -> None:
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        if self.root.winfo_exists():
            self.root.destroy()

    def _build(self) -> None:
        self.root.title("课业智析")
        self.root.geometry("520x330")
        self.root.minsize(480, 300)
        self.root.protocol("WM_DELETE_WINDOW", self._request_close)

        container = ttk.Frame(self.root, padding=20, name="content")
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="课业智析本地平台",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(container, textvariable=self._message).pack(
            anchor=tk.W,
            pady=(6, 16),
        )

        status = ttk.Frame(container, name="status")
        status.pack(fill=tk.X)
        self._status_row(status, 0, "Web 平台", self._web, "webStatus")
        self._status_row(
            status,
            1,
            "知识解析 Worker",
            self._ingestion,
            "ingestionStatus",
        )
        self._status_row(
            status,
            2,
            "事件投递 Worker",
            self._outbox,
            "outboxStatus",
        )
        ttk.Label(
            container,
            text="登录地址：http://127.0.0.1:8000/accounts/login/",
        ).pack(anchor=tk.W, pady=(18, 2))
        ttk.Label(
            container,
            text=f"日志目录：{self._controller.paths.logs_dir}",
            wraplength=470,
        ).pack(anchor=tk.W)

        actions = ttk.Frame(self.root, padding=(20, 0, 20, 18), name="actions")
        actions.pack(fill=tk.X)
        ttk.Button(
            actions,
            text="打开登录页",
            name="openLoginButton",
            command=self._controller.open_login_page,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="重新启动平台",
            name="restartButton",
            command=lambda: self._task_runner(self._controller.restart),
        ).pack(side=tk.LEFT, padx=8)
        ttk.Button(
            actions,
            text="停止平台",
            name="stopButton",
            command=lambda: self._task_runner(self._controller.stop),
        ).pack(side=tk.RIGHT)

    @staticmethod
    def _status_row(
        parent: ttk.Frame,
        row: int,
        label: str,
        value: tk.StringVar,
        name: str,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)
        ttk.Label(parent, textvariable=value, name=name).grid(
            row=row,
            column=1,
            sticky=tk.E,
            padx=(30, 0),
        )
        parent.columnconfigure(1, weight=1)

    def _poll(self) -> None:
        status = self._controller.status
        self._message.set(status.message)
        self._web.set(status.web)
        self._ingestion.set(status.ingestion)
        self._outbox.set(status.outbox)
        if status.phase == "admin_required" and self._admin_dialog is None:
            self._show_admin_dialog()
        if status.phase == "running":
            self._task_runner(self._controller.check_services)
        if self._close_when_stopped and status.phase in {"stopped", "failed"}:
            self.destroy()
            return
        self._poll_id = self.root.after(500, self._poll)

    def _show_admin_dialog(self) -> None:
        def submit(account_name: str, raw_password: str) -> None:
            self._admin_dialog = None
            self._task_runner(
                lambda: self._controller.create_admin_and_start(
                    account_name,
                    raw_password,
                )
            )

        def cancel() -> None:
            self._admin_dialog = None
            self._request_close()

        self._admin_dialog = InitialAdminDialog(
            self.root,
            on_submit=submit,
            on_cancel=cancel,
        )

    def _request_close(self) -> None:
        self._close_when_stopped = True
        self._task_runner(self._controller.stop)


class InitialAdminDialog:
    """Collect one administrator credential without retaining its password."""

    def __init__(
        self,
        parent: tk.Tk,
        *,
        on_submit: Callable[[str, str], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._on_submit = on_submit
        self._on_cancel = on_cancel
        self._error = tk.StringVar(value="")
        self.window = tk.Toplevel(parent, name="initialAdminDialog")
        self.window.title("创建首个管理员")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        form = ttk.Frame(self.window, padding=20, name="form")
        form.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            form,
            text="本系统尚无账户，请先创建管理员。",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 14))
        ttk.Label(form, text="账户名").grid(row=1, column=0, sticky=tk.W, pady=5)
        self._account = ttk.Entry(form, width=34, name="accountNameEntry")
        self._account.grid(row=1, column=1, pady=5)
        ttk.Label(form, text="密码").grid(row=2, column=0, sticky=tk.W, pady=5)
        self._password = ttk.Entry(
            form,
            width=34,
            show="●",
            name="passwordEntry",
        )
        self._password.grid(row=2, column=1, pady=5)
        ttk.Label(form, text="确认密码").grid(row=3, column=0, sticky=tk.W, pady=5)
        self._confirmation = ttk.Entry(
            form,
            width=34,
            show="●",
            name="confirmPasswordEntry",
        )
        self._confirmation.grid(row=3, column=1, pady=5)
        ttk.Label(form, textvariable=self._error, foreground="#b42318").grid(
            row=4,
            column=0,
            columnspan=2,
            sticky=tk.W,
            pady=(8, 4),
        )
        ttk.Button(form, text="取消", command=self._cancel).grid(
            row=5,
            column=0,
            sticky=tk.W,
            pady=(12, 0),
        )
        ttk.Button(
            form,
            text="创建管理员",
            name="submitButton",
            command=self._submit,
        ).grid(row=5, column=1, sticky=tk.E, pady=(12, 0))
        self._account.focus_set()
        self.window.grab_set()

    @property
    def error_text(self) -> str:
        return self._error.get()

    def _submit(self) -> None:
        account_name = self._account.get()
        raw_password = self._password.get()
        confirmation = self._confirmation.get()
        if not account_name.strip():
            self._error.set("账户名不能为空或全为空白")
            return
        if not raw_password.strip():
            self._error.set("密码不能为空或全为空白")
            return
        if raw_password != confirmation:
            self._error.set("两次输入的密码不一致")
            return
        self._password.delete(0, tk.END)
        self._confirmation.delete(0, tk.END)
        self.window.grab_release()
        self.window.withdraw()
        self._on_submit(account_name, raw_password)
        self.window.after_idle(self.window.destroy)

    def _cancel(self) -> None:
        self._password.delete(0, tk.END)
        self._confirmation.delete(0, tk.END)
        self.window.grab_release()
        self.window.destroy()
        self._on_cancel()


def _threaded_task_runner(task: Callable[[], None]) -> None:
    threading.Thread(target=task, daemon=True).start()

