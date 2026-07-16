"""Deterministic zero-argument M0 service stub."""

from pathlib import Path

from course_insight.modules.m0_platform.service import M0PlatformService


class M0PlatformServiceStub(M0PlatformService):
    """Instantiate M0 with fixed local-only dependency paths."""

    def __init__(self) -> None:
        super().__init__(
            database_path=Path("runtime/stub/course_insight.sqlite3"),
            runtime_dir=Path("runtime/stub"),
            config_dir=Path("config"),
        )
