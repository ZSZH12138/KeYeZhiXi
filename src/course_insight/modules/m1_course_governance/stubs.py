"""Deterministic local-only M1 service stub."""

import hashlib
from pathlib import Path

from course_insight.contracts.course import CoursePackage
from course_insight.modules.m1_course_governance.repository import M1Repository
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)


class _MemoryM1Repository:
    def __init__(self) -> None:
        self.packages: dict[tuple[str, str], CoursePackage] = {}

    def save_course_package(self, package: CoursePackage) -> None:
        self.packages[(package.course_package_id, package.package_version)] = package

    def get_course_package(
        self,
        course_package_id: str,
        package_version: str,
    ) -> CoursePackage | None:
        return self.packages.get((course_package_id, package_version))


def _read_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class M1CourseGovernanceServiceStub(M1CourseGovernanceService):
    """Instantiate M1 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        repository: M1Repository = _MemoryM1Repository()
        super().__init__({".md": _read_markdown}, _sha256, repository)
