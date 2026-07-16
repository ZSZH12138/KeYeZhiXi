"""M1 repository boundary for governed course packages."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.course import CoursePackage


_PACKAGE_TABLE = "m1_course_packages"


class M1Repository(Protocol):
    """Persistence operations owned exclusively by M1."""

    def save_course_package(self, package: CoursePackage) -> None:
        """Persist one immutable course-package version."""

    def get_course_package(
        self,
        course_package_id: str,
        package_version: str,
    ) -> CoursePackage | None:
        """Load one exact package version."""
