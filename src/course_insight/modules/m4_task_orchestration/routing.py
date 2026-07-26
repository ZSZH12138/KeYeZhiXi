"""Deterministic M4 task and blueprint routing rules."""

from __future__ import annotations

from collections.abc import Mapping

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.modules.m4_task_orchestration.intent_identity import (
    normalize_hint,
)
from course_insight.modules.m4_task_orchestration.intent_rules import (
    resolve_task_type_from_rules,
)


ASSESSMENT_TASK_TYPES = frozenset(
    {"diagnostic", "practice", "correction", "stage_assessment"}
)
SUPPORTED_TASK_TYPES = frozenset({"qa", *ASSESSMENT_TASK_TYPES})
ASSESSMENT_WORKFLOW = ("M8", "M2", "M7", "M5", "M6", "M9")
QA_WORKFLOW = ("M2", "M7", "M6")

def resolve_task_type(student_text: str, task_type_hint: str | None) -> str:
    """Resolve a supported task using hint-first deterministic rules."""

    normalized_text = " ".join(student_text.split()).casefold()
    if not normalized_text:
        _raise_unsupported("student task text must not be blank")
    if task_type_hint is not None:
        normalized_hint = normalize_hint(task_type_hint)
        if normalized_hint not in SUPPORTED_TASK_TYPES:
            _raise_unsupported(
                "task type is not supported",
                task_type=normalized_hint,
            )
        return normalized_hint
    resolved_task_type = resolve_task_type_from_rules(normalized_text)
    if resolved_task_type is not None:
        return resolved_task_type
    _raise_unsupported("student task text could not be classified")


def resolve_blueprint_id(
    *,
    task_type: str,
    course_id: str,
    knowledge_bundle: KnowledgeBundle,
    blueprint_by_task_type: Mapping[str, str],
) -> str | None:
    """Select the sole candidate or the explicit M4-owned mapping target."""

    if task_type not in ASSESSMENT_TASK_TYPES:
        return None
    candidates = tuple(
        blueprint
        for blueprint in knowledge_bundle.blueprints
        if blueprint.course_id == course_id
        and " ".join(blueprint.status.split()).casefold() == "teacher_approved"
    )
    configured_id = blueprint_by_task_type.get(task_type)
    if configured_id is not None:
        if any(
            blueprint.blueprint_id == configured_id for blueprint in candidates
        ):
            return configured_id
        _raise_blueprint_error(
            course_id=course_id,
            task_type=task_type,
            reason="configured_blueprint_unavailable",
            configured_blueprint_id=configured_id,
        )
    if len(candidates) == 1:
        return candidates[0].blueprint_id
    if not candidates:
        _raise_blueprint_error(
            course_id=course_id,
            task_type=task_type,
            reason="no_approved_blueprint",
        )
    _raise_blueprint_error(
        course_id=course_id,
        task_type=task_type,
        reason="selection_ambiguous",
        candidate_blueprint_ids=sorted(
            blueprint.blueprint_id for blueprint in candidates
        ),
    )


def workflow_for(task_type: str) -> list[str]:
    """Return an isolated frozen workflow for one resolved task type."""

    source = (
        ASSESSMENT_WORKFLOW
        if task_type in ASSESSMENT_TASK_TYPES
        else QA_WORKFLOW
    )
    return list(source)


def _raise_unsupported(message: str, **details: str) -> None:
    raise DomainError(
        code="UNSUPPORTED_TASK",
        module="m4",
        message=message,
        details=details,
        recoverable=True,
    )


def _raise_blueprint_error(
    *,
    course_id: str,
    task_type: str,
    reason: str,
    **details: object,
) -> None:
    raise DomainError(
        code="BLUEPRINT_NOT_FOUND",
        module="m4",
        message="assessment task requires an unambiguous approved blueprint",
        details={
            "course_id": course_id,
            "task_type": task_type,
            "reason": reason,
            **details,
        },
        recoverable=True,
    )
