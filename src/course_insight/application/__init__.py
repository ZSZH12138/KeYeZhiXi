"""Application orchestration boundary."""

from course_insight.application.coordinator import AppCoordinator
from course_insight.application.factory import (
    ApplicationContainer,
    RepositoryOverrides,
    ServiceOverrides,
    build_application,
)
from course_insight.application.runtime_context import (
    CourseRuntimeContext,
    CourseRuntimeRegistry,
    RuntimeSnapshotRefs,
)

__all__ = [
    "AppCoordinator",
    "ApplicationContainer",
    "CourseRuntimeContext",
    "CourseRuntimeRegistry",
    "RepositoryOverrides",
    "RuntimeSnapshotRefs",
    "ServiceOverrides",
    "build_application",
]
