"""Lazy, process-local composition root for the M0 Django boundary."""

from __future__ import annotations

import atexit
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Mapping, Protocol, cast

from course_insight.application.factory import (
    ApplicationContainer,
    build_application,
)
from course_insight.application.runtime_context import (
    CourseRuntimeContext,
    RuntimeSnapshotRefs,
)
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import read_json
from course_insight.infrastructure.logging import (
    LoggingRuntime,
    configure_application_logging,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    StatePolicy,
)
from course_insight.modules.m9_teacher_analytics.suggestions import (
    TeacherThresholdPolicy,
)


_MANIFEST_RELATIVE_PATH = Path(
    "snapshots/course_runtime_manifest.json"
)
_MANIFEST_KEYS = frozenset({"schema_version", "courses"})
_COURSE_KEYS = frozenset(
    {
        "course_id",
        "course_package_ref",
        "evidence_index_ref",
        "knowledge_bundle_ref",
        "state_policy_ref",
        "teacher_threshold_policy_ref",
    }
)
_COURSE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_POLICY_BYTES = 1024 * 1024
_MAX_COURSES = 10_000


class _RuntimeRegistry(Protocol):
    def restore(
        self,
        course_id: str,
        refs: RuntimeSnapshotRefs,
    ) -> CourseRuntimeContext:
        """Restore one validated course context."""


class _ContainerLike(Protocol):
    runtime_registry: _RuntimeRegistry


@dataclass(frozen=True, slots=True)
class WebCourseRuntime:
    """Contracts and policy references required by one Web course flow."""

    course_id: str
    course_context: CourseRuntimeContext | None
    state_policy_path: Path
    teacher_threshold_policy_path: Path


@dataclass(frozen=True, slots=True)
class WebRuntime:
    """One immutable graph shared by all requests in a Django process."""

    container: ApplicationContainer
    courses: Mapping[str, WebCourseRuntime]

    def require_course(self, course_id: str) -> WebCourseRuntime:
        context = self.courses.get(course_id)
        if context is None:
            context = _dynamic_course_runtime(course_id)
        return WebCourseRuntime(
            course_id=context.course_id,
            course_context=(
                None
                if context.course_context is None
                else context.course_context.isolated_copy()
            ),
            state_policy_path=context.state_policy_path,
            teacher_threshold_policy_path=(
                context.teacher_threshold_policy_path
            ),
        )

    def logging_is_ready(self) -> bool:
        runtime = _LOGGING_RUNTIME
        return (
            runtime is not None
            and runtime.handler in runtime.logger.handlers
        )


@dataclass(frozen=True, slots=True)
class _ManifestCourse:
    course_id: str
    course_package_ref: Path
    evidence_index_ref: Path
    knowledge_bundle_ref: Path
    state_policy_path: Path
    teacher_threshold_policy_path: Path


_LOCK = RLock()
_CONTAINER: ApplicationContainer | None = None
_WEB_RUNTIME: WebRuntime | None = None
_LOGGING_RUNTIME: LoggingRuntime | None = None
_ATEXIT_REGISTERED = False
OUTBOX_WORKER_LOG_FILENAME = "outbox.log"
INGESTION_WORKER_LOG_FILENAME = "ingestion.log"


def get_application_container(
    *,
    logging_filename: str | None = None,
) -> ApplicationContainer:
    """Build, initialize, and cache exactly one owned dependency graph."""

    global _CONTAINER, _LOGGING_RUNTIME, _ATEXIT_REGISTERED
    with _LOCK:
        if _CONTAINER is not None:
            return _CONTAINER
        from django.conf import settings as django_settings

        container = build_application(django_settings.PLATFORM_SETTINGS)
        logging_runtime: LoggingRuntime | None = None
        logging_settings = django_settings.PLATFORM_SETTINGS.logging
        if (
            logging_filename is not None
            and logging_settings.mode == "rotating_file"
            and logging_settings.filename != logging_filename
        ):
            logging_settings = logging_settings.model_copy(
                update={"filename": logging_filename}
            )
        try:
            container.m0_service.initialize()
            logging_runtime = configure_application_logging(logging_settings)
        except Exception:
            if logging_runtime is not None:
                logging_runtime.close()
            container.close()
            raise
        _CONTAINER = container
        _LOGGING_RUNTIME = logging_runtime
        if not _ATEXIT_REGISTERED:
            atexit.register(close_application_container)
            _ATEXIT_REGISTERED = True
        return container


def get_web_runtime() -> WebRuntime:
    """Restore the fixed manifest once after successful composition."""

    global _WEB_RUNTIME
    with _LOCK:
        if _WEB_RUNTIME is not None:
            return _WEB_RUNTIME
        container = get_application_container()
        courses = restore_course_runtime_manifest(
            container=container,
            runtime_dir=container.settings.runtime_dir,
        )
        _WEB_RUNTIME = WebRuntime(
            container=container,
            courses=courses,
        )
        return _WEB_RUNTIME


def close_application_container() -> None:
    """Idempotently release logging and database resources."""

    global _CONTAINER, _WEB_RUNTIME, _LOGGING_RUNTIME
    with _LOCK:
        container = _CONTAINER
        logging_runtime = _LOGGING_RUNTIME
        _CONTAINER = None
        _WEB_RUNTIME = None
        _LOGGING_RUNTIME = None
    if logging_runtime is not None:
        logging_runtime.close()
    if container is not None:
        container.close()


def close_web_runtime() -> None:
    """Backward-compatible explicit Web process cleanup."""

    close_application_container()


def restore_course_runtime_manifest(
    *,
    container: _ContainerLike,
    runtime_dir: Path,
) -> Mapping[str, WebCourseRuntime]:
    """Validate all refs and policies, then publish restored contexts."""

    runtime_root = Path(runtime_dir).resolve()
    manifest_path = (runtime_root / _MANIFEST_RELATIVE_PATH).resolve()
    if not manifest_path.is_relative_to(runtime_root):
        _invalid_manifest("unsafe_manifest")
    try:
        if (
            not manifest_path.is_file()
            or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES
        ):
            _invalid_manifest("manifest_unavailable")
        payload = read_json(manifest_path)
        courses = _validate_manifest(payload, runtime_root=runtime_root)
        for course in courses:
            _validate_policy_files(course)
    except DomainError as error:
        if error.code == "RUNTIME_MANIFEST_INVALID":
            raise
        _invalid_manifest("policy_invalid", cause=error)
    except Exception as error:
        _invalid_manifest("manifest_invalid", cause=error)

    restored: dict[str, WebCourseRuntime] = {}
    try:
        for course in courses:
            context = container.runtime_registry.restore(
                course.course_id,
                RuntimeSnapshotRefs(
                    course_package_ref=course.course_package_ref,
                    evidence_index_ref=course.evidence_index_ref,
                    knowledge_bundle_ref=course.knowledge_bundle_ref,
                ),
            )
            restored[course.course_id] = WebCourseRuntime(
                course_id=course.course_id,
                course_context=context,
                state_policy_path=course.state_policy_path,
                teacher_threshold_policy_path=(
                    course.teacher_threshold_policy_path
                ),
            )
    except DomainError:
        raise
    except Exception as error:
        _invalid_manifest("runtime_restore_failed", cause=error)
    return MappingProxyType(restored)


def _validate_manifest(
    payload: object,
    *,
    runtime_root: Path,
) -> tuple[_ManifestCourse, ...]:
    if (
        type(payload) is not dict
        or set(payload) != _MANIFEST_KEYS
        or payload.get("schema_version") != 1
        or type(payload.get("courses")) is not list
        or not payload["courses"]
        or len(payload["courses"]) > _MAX_COURSES
    ):
        _invalid_manifest("manifest_shape")
    parsed: list[_ManifestCourse] = []
    course_ids: set[str] = set()
    for value in cast(list[object], payload["courses"]):
        if type(value) is not dict or set(value) != _COURSE_KEYS:
            _invalid_manifest("course_shape")
        course = cast(dict[str, object], value)
        course_id = course["course_id"]
        if (
            type(course_id) is not str
            or not _COURSE_ID.fullmatch(course_id)
            or course_id in course_ids
        ):
            _invalid_manifest("course_identity")
        course_ids.add(course_id)
        parsed.append(
            _ManifestCourse(
                course_id=course_id,
                course_package_ref=_relative_reference(
                    course["course_package_ref"],
                    runtime_root=runtime_root,
                    resolved=False,
                ),
                evidence_index_ref=_relative_reference(
                    course["evidence_index_ref"],
                    runtime_root=runtime_root,
                    resolved=False,
                ),
                knowledge_bundle_ref=_relative_reference(
                    course["knowledge_bundle_ref"],
                    runtime_root=runtime_root,
                    resolved=False,
                ),
                state_policy_path=_relative_reference(
                    course["state_policy_ref"],
                    runtime_root=runtime_root,
                    resolved=True,
                ),
                teacher_threshold_policy_path=_relative_reference(
                    course["teacher_threshold_policy_ref"],
                    runtime_root=runtime_root,
                    resolved=True,
                ),
            )
        )
    return tuple(parsed)


def _relative_reference(
    value: object,
    *,
    runtime_root: Path,
    resolved: bool,
) -> Path:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 512
    ):
        _invalid_manifest("reference_shape")
    path = Path(value)
    if (
        path.is_absolute()
        or path.drive
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _invalid_manifest("unsafe_reference")
    target = (runtime_root / path).resolve()
    if not target.is_relative_to(runtime_root):
        _invalid_manifest("unsafe_reference")
    return target if resolved else path


def _validate_policy_files(course: _ManifestCourse) -> None:
    paths = (
        course.state_policy_path,
        course.teacher_threshold_policy_path,
    )
    if any(
        not path.is_file() or path.stat().st_size > _MAX_POLICY_BYTES
        for path in paths
    ):
        _invalid_manifest("policy_unavailable")
    try:
        StatePolicy.from_path(course.state_policy_path)
        TeacherThresholdPolicy.from_path(
            course.teacher_threshold_policy_path
        )
    except Exception as error:
        _invalid_manifest("policy_invalid", cause=error)


def _dynamic_course_runtime(course_id: str) -> WebCourseRuntime:
    if not isinstance(course_id, str) or not _COURSE_ID.fullmatch(course_id):
        raise DomainError(
            code="COURSE_RUNTIME_NOT_FOUND",
            module="m0",
            message="course runtime context was not published",
        )
    from django.conf import settings as django_settings

    from course_insight.modules.m0_platform.django_app.models import (
        CourseClassWorkspace,
    )

    if not CourseClassWorkspace.objects.filter(
        course_id=course_id,
        status=CourseClassWorkspace.Status.ACTIVE,
    ).exists():
        raise DomainError(
            code="COURSE_RUNTIME_NOT_FOUND",
            module="m0",
            message="course runtime context was not published",
        )
    config_dir = Path(django_settings.PLATFORM_SETTINGS.config_dir).resolve()
    return WebCourseRuntime(
        course_id=course_id,
        course_context=None,
        state_policy_path=config_dir / "state.json",
        teacher_threshold_policy_path=config_dir / "teacher.json",
    )


def _invalid_manifest(
    reason: str,
    *,
    cause: BaseException | None = None,
) -> None:
    error = DomainError(
        code="RUNTIME_MANIFEST_INVALID",
        module="m0",
        message="course runtime manifest is invalid",
        details={"reason": reason},
        recoverable=True,
    )
    if cause is None:
        raise error
    raise error from cause
