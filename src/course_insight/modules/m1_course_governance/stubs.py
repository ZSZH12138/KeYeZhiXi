"""Deterministic local-only M1 service stub."""

import hashlib
from copy import deepcopy

from course_insight.contracts.course import CoursePackage
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
)
from course_insight.modules.m1_course_governance.repository import M1Repository
from course_insight.modules.m1_course_governance.parsers import parse_source
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)


class _MemoryM1Repository:
    def __init__(self) -> None:
        self.packages: dict[tuple[str, str], CoursePackage] = {}
        self.snapshots: dict[tuple[str, str], CourseImportSnapshot] = {}

    def save_course_package(self, package: CoursePackage) -> None:
        self.packages[(package.course_package_id, package.package_version)] = (
            package.model_copy(deep=True)
        )

    def save_course_import(
        self,
        package: CoursePackage,
        snapshot: CourseImportSnapshot,
    ) -> None:
        key = (package.course_package_id, package.package_version)
        self.packages[key] = package.model_copy(deep=True)
        self.snapshots[key] = deepcopy(snapshot)

    def get_course_package(
        self,
        course_package_id: str,
        package_version: str,
    ) -> CoursePackage | None:
        package = self.packages.get((course_package_id, package_version))
        return None if package is None else package.model_copy(deep=True)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class M1CourseGovernanceServiceStub(M1CourseGovernanceService):
    """Instantiate M1 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        repository: M1Repository = _MemoryM1Repository()
        super().__init__(
            {
                ".md": parse_source,
                ".txt": parse_source,
                ".pdf": parse_source,
                ".docx": parse_source,
                ".pptx": parse_source,
            },
            _sha256,
            repository,
        )
