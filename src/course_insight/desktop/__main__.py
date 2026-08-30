"""Command-line and frozen entry point for the Windows desktop app."""

from __future__ import annotations

import argparse
import os
import webbrowser
from io import StringIO
from typing import Any

from course_insight.desktop.bootstrap import (
    configure_process_environment,
    migrate_database,
    prepare_installation,
)
from course_insight.desktop.controller import DesktopController, LOGIN_URL
from course_insight.desktop.logging import configure_launcher_logging
from course_insight.desktop.paths import resolve_desktop_paths
from course_insight.desktop.services import SERVICE_NAMES, run_service
from course_insight.desktop.single_instance import SingleInstance
from course_insight.desktop.supervisor import ServiceSupervisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="KeYeZhiXi")
    parser.add_argument("--service", choices=SERVICE_NAMES)
    parser.add_argument("--diagnose", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.service:
        run_service(arguments.service)
        return 0
    if arguments.diagnose:
        return run_diagnostics()
    return run_desktop()


def run_desktop() -> int:
    instance = SingleInstance()
    if not instance.acquire():
        webbrowser.open(LOGIN_URL)
        return 0
    try:
        return _run_primary_desktop()
    finally:
        instance.release()


def _run_primary_desktop() -> int:
    import tkinter as tk
    from tkinter import messagebox

    from course_insight.desktop.ui import DesktopWindow

    controller: DesktopController | None = None
    root: tk.Tk | None = None
    try:
        paths = resolve_desktop_paths(os.environ)
        prepare_installation(paths)
        configure_launcher_logging(paths.logs_dir)
        root = tk.Tk()
        supervisor = ServiceSupervisor(paths)
        controller = DesktopController(paths, supervisor)
        DesktopWindow(root, controller).run()
        return 0
    except Exception:
        if root is None:
            root = tk.Tk()
            root.withdraw()
        messagebox.showerror(
            "课业智析启动失败",
            "平台无法启动，请检查本地应用数据目录中的日志。",
            parent=root,
        )
        return 1
    finally:
        if controller is not None:
            controller.stop()
        if root is not None:
            _destroy_root(root)


def _destroy_root(root: Any) -> None:
    """Make final Tk cleanup safe after the UI already ended its mainloop."""

    import tkinter as tk

    try:
        if root.winfo_exists():
            root.destroy()
    except tk.TclError:
        pass


def run_diagnostics() -> int:
    """Exercise migrations, Django checks, and one bounded Worker cycle."""

    paths = resolve_desktop_paths(os.environ)
    prepare_installation(paths)
    configure_launcher_logging(paths.logs_dir)
    configure_process_environment(paths)
    migrate_database()

    from django.core.management import call_command
    from django.db import connections

    from course_insight.modules.m0_platform.django_app import runtime

    sink = StringIO()
    try:
        call_command("check", verbosity=0, stdout=sink, stderr=sink)
        call_command(
            "run_ingestion_worker",
            once=True,
            verbosity=0,
            stdout=sink,
            stderr=sink,
        )
        call_command(
            "run_outbox_worker",
            once=True,
            verbosity=0,
            stdout=sink,
            stderr=sink,
        )
    finally:
        runtime.close_application_container()
        runtime.close_web_runtime()
        connections.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
