"""Resolve the evidence index frozen to one release-backed assessment."""

from __future__ import annotations

from django.core.exceptions import ValidationError

from course_insight.contracts.errors import DomainError
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m0_platform.django_app.models import (
    CourseKnowledgeRelease,
)
from course_insight.modules.m3_knowledge_bundle.release_compatibility import (
    course_package_from_release,
)


def evidence_index_for_assessment(
    web_runtime,
    course,
    *,
    task: object,
    knowledge_bundle,
    course_id: str,
    class_id: str,
):
    """Load or build the index belonging to the assessment's frozen release."""

    course_context = getattr(course, "course_context", None)
    configured = (
        None if course_context is None else course_context.evidence_index_ref
    )
    if (
        configured is not None
        and configured.course_package_id == knowledge_bundle.course_package_id
    ):
        return configured
    if type(task) is not TaskPlan:
        if configured is None:
            raise DomainError(
                code="RUNTIME_CONTEXT_UNAVAILABLE",
                module="m0",
                message="课程运行配置不可用。",
                recoverable=True,
            )
        return configured
    release = _release_for_task(
        task,
        course_id=course_id,
        class_id=class_id,
    )
    package = course_package_from_release(release)
    builder = getattr(web_runtime.container.m2_service, "build_index", None)
    if not callable(builder):
        raise DomainError(
            code="RUNTIME_CONTEXT_UNAVAILABLE",
            module="m0",
            message="release evidence indexing is unavailable",
            recoverable=True,
        )
    index_ref = builder(package)
    if index_ref.course_package_id != knowledge_bundle.course_package_id:
        raise DomainError(
            code="WORKFLOW_DEPENDENCY_MISMATCH",
            module="m0",
            message="release evidence index is not assessment-aligned",
            recoverable=True,
        )
    return index_ref


def _release_for_task(
    task: TaskPlan,
    *,
    course_id: str,
    class_id: str,
) -> CourseKnowledgeRelease:
    try:
        release = CourseKnowledgeRelease.objects.filter(
            pk=task.knowledge_bundle_id,
            course_id=course_id,
            class_id=class_id,
            status__in=(
                CourseKnowledgeRelease.Status.ACTIVE,
                CourseKnowledgeRelease.Status.RETIRED,
            ),
        ).first()
    except (ValidationError, ValueError):
        release = None
    if release is None:
        raise DomainError(
            code="FROZEN_KNOWLEDGE_RELEASE_MISSING",
            module="m0",
            message="该试卷绑定的课程版本已不可用。",
        )
    return release


__all__ = ["evidence_index_for_assessment"]
