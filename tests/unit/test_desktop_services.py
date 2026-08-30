from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.desktop import services
from course_insight.desktop.services import (
    DesktopServiceError,
    build_service_command,
    run_service,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "web",
            (str(Path("C:/app/KeYeZhiXi.exe")), "--service", "web"),
        ),
        (
            "ingestion",
            (
                str(Path("C:/app/KeYeZhiXi.exe")),
                "--service",
                "ingestion",
            ),
        ),
        (
            "outbox",
            (str(Path("C:/app/KeYeZhiXi.exe")), "--service", "outbox"),
        ),
    ],
)
def test_frozen_service_command_reuses_the_same_executable(
    name: str,
    expected: tuple[str, ...],
) -> None:
    assert build_service_command(
        name,
        frozen=True,
        executable=Path("C:/app/KeYeZhiXi.exe"),
        python_executable=Path("C:/Python/python.exe"),
    ) == expected


def test_source_service_command_uses_the_desktop_module() -> None:
    assert build_service_command(
        "web",
        frozen=False,
        executable=Path("C:/app/unused.exe"),
        python_executable=Path("C:/Python/python.exe"),
    ) == (
        str(Path("C:/Python/python.exe")),
        "-m",
        "course_insight.desktop",
        "--service",
        "web",
    )


def test_service_command_rejects_unknown_internal_modes() -> None:
    with pytest.raises(DesktopServiceError, match="未知的内部服务"):
        build_service_command(
            "malicious --argument",
            frozen=True,
            executable=Path("C:/app/KeYeZhiXi.exe"),
            python_executable=Path("C:/Python/python.exe"),
        )


@pytest.mark.parametrize(
    ("name", "command", "args", "options"),
    [
        (
            "web",
            "runserver",
            ("127.0.0.1:8000",),
            {"use_reloader": False},
        ),
        ("ingestion", "run_ingestion_worker", (), {}),
        ("outbox", "run_outbox_worker", (), {}),
    ],
)
def test_internal_service_maps_to_the_existing_management_command(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    command: str,
    args: tuple[str, ...],
    options: dict[str, object],
) -> None:
    calls: list[tuple[str, tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(services, "_ensure_django_ready", lambda: None)
    monkeypatch.setattr(
        services,
        "call_command",
        lambda selected, *values, **kwargs: calls.append(
            (selected, values, kwargs)
        ),
    )

    run_service(name)

    assert calls == [(command, args, options)]
