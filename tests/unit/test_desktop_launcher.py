from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from course_insight.desktop_launcher import (
    child_command,
    configure_bootstrap_administrator,
    ensure_product_layout,
    launch_platform_processes,
    management_arguments,
    write_launcher_failure,
)
from course_insight.modules.m0_platform.django_app.models import AccountType, User


def test_first_launch_creates_local_configuration_without_overwriting_it(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.example.json").write_text(
        '{"environment":"development"}',
        encoding="utf-8",
    )
    (config_dir / "roles.example.csv").write_text(
        "actor_id,role,course_id,class_id,is_active\n"
        "pseudonym_system_admin_001,system_admin,,,true\n",
        encoding="utf-8",
    )

    ensure_product_layout(tmp_path, secret_factory=lambda: "first-secret")

    assert (config_dir / "app.json").read_text(encoding="utf-8") == (
        '{"environment":"development"}'
    )
    assert (config_dir / "roles.csv").read_text(encoding="utf-8").endswith(
        "pseudonym_system_admin_001,system_admin,,,true\n"
    )
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "DJANGO_SECRET_KEY=first-secret\n"
    )
    assert (tmp_path / "runtime").is_dir()

    (config_dir / "app.json").write_text("custom-app", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "DJANGO_SECRET_KEY=custom-secret\n",
        encoding="utf-8",
    )
    ensure_product_layout(
        tmp_path,
        secret_factory=lambda: (_ for _ in ()).throw(
            AssertionError("existing secret must not be replaced")
        ),
    )

    assert (config_dir / "app.json").read_text(encoding="utf-8") == "custom-app"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == (
        "DJANGO_SECRET_KEY=custom-secret\n"
    )


def test_child_command_uses_module_entrypoint_in_source_checkout() -> None:
    executable = Path("C:/Python/python.exe")

    assert child_command("web", executable=executable, frozen=False) == [
        str(executable),
        "-m",
        "course_insight.desktop_launcher",
        "web",
    ]


def test_child_command_reuses_packaged_executable_in_frozen_build() -> None:
    executable = Path("C:/Product/课业智析.exe")

    assert child_command("outbox", executable=executable, frozen=True) == [
        str(executable),
        "outbox",
    ]


@pytest.mark.django_db
def test_first_launch_sets_readable_administrator_credentials_once() -> None:
    administrator = User(
        username="pseudonym_system_admin_001",
        actor_id="pseudonym_system_admin_001",
        account_type=AccountType.ADMINISTRATOR,
        email="",
        first_name="",
        last_name="",
    )
    administrator.set_unusable_password()
    administrator.save()
    accounts = iter(["school_admin"])
    passwords = iter(["A-secure-local-password", "A-secure-local-password"])

    changed = configure_bootstrap_administrator(
        account_reader=lambda _: next(accounts),
        password_reader=lambda _: next(passwords),
        writer=lambda _: None,
    )

    administrator.refresh_from_db()
    assert changed is True
    assert administrator.username == "school_admin"
    assert administrator.check_password("A-secure-local-password")

    changed_again = configure_bootstrap_administrator(
        account_reader=lambda _: (_ for _ in ()).throw(
            AssertionError("configured account must not be requested again")
        ),
        password_reader=lambda _: (_ for _ in ()).throw(
            AssertionError("configured password must not be requested again")
        ),
        writer=lambda _: None,
    )
    assert changed_again is False


def test_launcher_starts_three_processes_records_them_then_opens_login(
    tmp_path: Path,
) -> None:
    executable = Path("C:/Product/课业智析.exe")
    calls: list[tuple[list[str], Path]] = []
    opened: list[str] = []
    checked: list[tuple[str, float]] = []

    def process_factory(command, *, cwd, **_):
        calls.append((list(command), Path(cwd)))
        return SimpleNamespace(pid=4100 + len(calls))

    pids = launch_platform_processes(
        tmp_path,
        executable=executable,
        frozen=True,
        process_factory=process_factory,
        health_waiter=lambda url, timeout: not checked.append((url, timeout)),
        browser_opener=opened.append,
    )

    assert calls == [
        ([str(executable), "ingestion"], tmp_path),
        ([str(executable), "outbox"], tmp_path),
        ([str(executable), "web"], tmp_path),
    ]
    assert pids == (4101, 4102, 4103)
    assert json.loads(
        (tmp_path / "runtime" / "desktop_processes.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "ingestion": 4101,
        "outbox": 4102,
        "web": 4103,
    }
    assert checked == [("http://127.0.0.1:8000/accounts/login/", 30.0)]
    assert opened == ["http://127.0.0.1:8000/accounts/login/"]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("web", ("runserver", "127.0.0.1:8000", "--noreload")),
        ("ingestion", ("run_ingestion_worker",)),
        ("outbox", ("run_outbox_worker",)),
    ],
)
def test_process_modes_map_to_bounded_management_commands(
    mode: str,
    expected: tuple[str, ...],
) -> None:
    assert management_arguments(mode) == expected


def test_process_mode_rejects_unknown_management_command() -> None:
    with pytest.raises(ValueError, match="unsupported process mode"):
        management_arguments("shell")


def test_launcher_failure_writes_local_diagnostic_traceback(tmp_path: Path) -> None:
    try:
        raise NameError("broken startup")
    except NameError as error:
        log_path = write_launcher_failure(tmp_path, error)

    content = log_path.read_text(encoding="utf-8")
    assert log_path == tmp_path / "runtime" / "launcher-error.log"
    assert "NameError: broken startup" in content


@pytest.mark.parametrize("name", ["启动课业智析.cmd", "停止课业智析.cmd"])
def test_windows_cmd_launchers_use_crlf_line_endings(name: str) -> None:
    data = Path(name).read_bytes()

    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
