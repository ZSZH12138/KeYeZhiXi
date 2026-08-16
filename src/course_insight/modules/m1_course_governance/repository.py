"""M1 repository boundary for governed course packages."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.course import CoursePackage
from course_insight.modules.m1_course_governance.snapshots import CourseImportSnapshot


_PACKAGE_TABLE = "m1_course_packages"


class M1Repository(Protocol):
    """Persistence operations owned exclusively by M1."""

    def save_course_package(self, package: CoursePackage) -> None:
        """Persist one immutable course-package version."""

    def save_course_import(
        self, package: CoursePackage, snapshot: CourseImportSnapshot
    ) -> None:
        """Persist a complete immutable package and its authorized raw inputs."""

    def get_course_package(
        self,
        course_package_id: str,
        package_version: str,
    ) -> CoursePackage | None:
        """Load one exact package version."""
