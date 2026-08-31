"""Windows desktop launcher shared by source and packaged distributions."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import signal
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any


PROCESS_MODES = frozenset({"web", "ingestion", "outbox"})
BOOTSTRAP_ACTOR_ID = "pseudonym_system_admin_001"


def ensure_product_layout(
    product_root: Path,
    *,
    secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(48),
) -> None:
    """Create first-run local files without replacing operator-owned values."""

    root = product_root.resolve()
    config_dir = root / "config"
    runtime_dir = root / "runtime"
    config_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    _copy_if_missing(config_dir / "app.example.json", config_dir / "app.json")
    _copy_if_missing(config_dir / "roles.example.csv", config_dir / "roles.csv")
    _create_secret_if_missing(root / ".env", secret_factory=secret_factory)


def child_command(
    mode: str,
    *,
    executable: Path,
    frozen: bool,
) -> list[str]:
    """Return one child command for a validated platform process mode."""

    if mode not in PROCESS_MODES:
        raise ValueError(f"unsupported process mode: {mode}")
    if frozen:
        return [str(executable), mode]
    return [
        str(executable),
        "-m",
        "course_insight.desktop_launcher",
        mode,
    ]


def management_arguments(mode: str) -> tuple[str, ...]:
    """Map public process modes to the only allowed Django commands."""

    commands = {
        "web": ("runserver", "127.0.0.1:8000", "--noreload"),
        "ingestion": ("run_ingestion_worker",),
        "outbox": ("run_outbox_worker",),
    }
    try:
        return commands[mode]
    except KeyError:
        raise ValueError(f"unsupported process mode: {mode}") from None


def configure_bootstrap_administrator(
    *,
    account_reader: Callable[[str], str] = input,
    password_reader: Callable[[str], str],
    writer: Callable[[str], object] = print,
) -> bool:
    """Set the first readable administrator login without storing plaintext."""

    from django.core.exceptions import ValidationError
    from django.db import IntegrityError, transaction

    from course_insight.modules.m0_platform.django_app.models import User

    administrator = User.objects.filter(actor_id=BOOTSTRAP_ACTOR_ID).first()
    if administrator is None:
        raise RuntimeError("bootstrap administrator is unavailable")
    if administrator.has_usable_password():
        return False

    writer("首次启动：请设置平台管理员账号和密码。")
    while True:
        account_name = account_reader("管理员账号：").strip()
        if not account_name:
            writer("管理员账号不能为空。")
            continue
        password = password_reader("管理员密码（至少 12 个字符）：")
        confirmation = password_reader("再次输入管理员密码：")
        if len(password) < 12:
            writer("管理员密码至少需要 12 个字符。")
            continue
        if password != confirmation:
            writer("两次输入的密码不一致。")
            continue
        try:
            with transaction.atomic():
                locked = User.objects.select_for_update().get(
                    actor_id=BOOTSTRAP_ACTOR_ID
                )
                if locked.has_usable_password():
                    return False
                locked.username = account_name
                locked.set_password(password)
                locked.full_clean()
                locked.save(update_fields=("username", "username_digest", "password"))
        except (IntegrityError, ValidationError):
            writer("该管理员账号不可用，请换一个账号名。")
            continue
        writer("管理员初始化完成。")
        return True


def launch_platform_processes(
    product_root: Path,
    *,
    executable: Path = Path(sys.executable),
    frozen: bool = bool(getattr(sys, "frozen", False)),
    process_factory: Callable[..., Any] = subprocess.Popen,
    health_waiter: Callable[[str, float], bool] | None = None,
    browser_opener: Callable[[str], object] = webbrowser.open,
) -> tuple[int, ...]:
    """Launch the two workers and Web process, then open the login page."""

    root = product_root.resolve()
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    environment = _launcher_environment(root)
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    startup_info = _minimized_startup_info()
    processes: list[tuple[str, Any]] = []
    for mode in ("ingestion", "outbox", "web"):
        process = process_factory(
            child_command(mode, executable=executable, frozen=frozen),
            cwd=root,
            env=environment,
            creationflags=creation_flags,
            startupinfo=startup_info,
        )
        processes.append((mode, process))

    pid_payload = {mode: int(process.pid) for mode, process in processes}
    _write_json_atomically(runtime_dir / "desktop_processes.json", pid_payload)
    login_url = "http://127.0.0.1:8000/accounts/login/"
    waiter = wait_for_web if health_waiter is None else health_waiter
    if not waiter(login_url, 30.0):
        for _, process in reversed(processes):
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                terminate()
        raise RuntimeError("platform did not become ready")
    browser_opener(login_url)
    return tuple(pid_payload.values())


def wait_for_web(url: str, timeout_seconds: float) -> bool:
    """Wait until the local login page returns a successful HTTP response."""

    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 400:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    return False


def resolve_product_root() -> Path:
    """Locate mutable configuration beside the source tree or packaged EXE."""

    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def prepare_product(product_root: Path) -> None:
    """Create local configuration, migrate storage, and initialize the admin."""

    root = product_root.resolve()
    ensure_product_layout(root)
    os.environ.update(_launcher_environment(root))
    os.chdir(root)
    import django
    from django.core.management import call_command

    django.setup()
    call_command("migrate", interactive=False, verbosity=0)
    call_command("sync_roles", apply=True, verbosity=0)
    configure_bootstrap_administrator(password_reader=getpass.getpass)


def execute_process_mode(mode: str, product_root: Path) -> int:
    """Run exactly one long-lived Django process in the current console."""

    root = product_root.resolve()
    ensure_product_layout(root)
    os.environ.update(_launcher_environment(root))
    os.chdir(root)
    from django.core.management import execute_from_command_line

    execute_from_command_line(["course-insight", *management_arguments(mode)])
    return 0


def stop_platform_processes(product_root: Path) -> int:
    """Stop only PIDs recorded by this product's desktop launcher."""

    pid_path = product_root.resolve() / "runtime" / "desktop_processes.json"
    if not pid_path.is_file():
        return 0
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("desktop process record is invalid") from None
    if not isinstance(payload, dict):
        raise RuntimeError("desktop process record is invalid")
    pids = []
    for value in payload.values():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RuntimeError("desktop process record is invalid")
        pids.append(value)
    for pid in reversed(tuple(dict.fromkeys(pids))):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    pid_path.unlink(missing_ok=True)
    return len(set(pids))


def write_launcher_failure(product_root: Path, error: Exception) -> Path:
    """Persist a local traceback for support without printing it to users."""

    runtime_dir = product_root.resolve() / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "launcher-error.log"
    path.write_text(
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
        encoding="utf-8",
    )
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="课业智析")
    parser.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "command",
        choices=("start", "stop", "prepare", *sorted(PROCESS_MODES)),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch the desktop product lifecycle without arbitrary commands."""

    args = _parser().parse_args(argv)
    root = resolve_product_root() if args.root is None else args.root.resolve()
    try:
        if args.command == "prepare":
            prepare_product(root)
            return 0
        if args.command == "start":
            prepare_product(root)
            launch_platform_processes(root)
            return 0
        if args.command == "stop":
            return 0 if stop_platform_processes(root) >= 0 else 1
        return execute_process_mode(args.command, root)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        write_launcher_failure(root, error)
        print(f"启动失败：{type(error).__name__}", file=sys.stderr)
        return 1


def _launcher_environment(product_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "course_insight.web_project.settings",
            "DJANGO_COURSE_INSIGHT_DOTENV_PATH": str(product_root / ".env"),
            "DJANGO_COURSE_INSIGHT_APP_JSON_PATH": str(
                product_root / "config" / "app.json"
            ),
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _minimized_startup_info():
    if os.name != "nt":
        return None
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = 6  # SW_MINIMIZE
    return startup_info


def _write_json_atomically(path: Path, payload: dict[str, int]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _copy_if_missing(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(source.read_bytes())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _create_secret_if_missing(
    path: Path,
    *,
    secret_factory: Callable[[], str],
) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        return
    try:
        secret = secret_factory()
        if not secret or "\r" in secret or "\n" in secret:
            raise ValueError("generated secret is invalid")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(f"DJANGO_SECRET_KEY={secret}\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
