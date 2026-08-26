"""Exact course/class DeepSeek settings with write-only secret handling."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction

from course_insight.infrastructure.deepseek import (
    DEEPSEEK_BASE_URL,
    SUPPORTED_DEEPSEEK_MODELS,
)
from course_insight.infrastructure.scoped_credentials import (
    ProtectedSecret,
    protect_secret,
    unprotect_secret,
)
from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    SCOPE_ID_PATTERN,
    ScopedDeepSeekConfiguration,
)


_scope_pattern = re.compile(SCOPE_ID_PATTERN)
_MAX_KEY_LENGTH = 256


@dataclass(frozen=True, slots=True)
class ScopedDeepSeekPublicStatus:
    configured: bool
    masked_key: str
    endpoint: str
    model_name: str
    thinking_enabled: bool
    api_revision: int


@dataclass(frozen=True, slots=True)
class ScopedDeepSeekSettings:
    api_key: str
    model_name: str
    thinking_enabled: bool
    api_revision: int


def scoped_deepseek_status(
    course_id: str,
    class_id: str,
) -> ScopedDeepSeekPublicStatus:
    _validate_scope(course_id, class_id)
    workspace = CourseClassWorkspace.objects.filter(
        course_id=course_id,
        class_id=class_id,
    ).first()
    configuration = (
        None
        if workspace is None
        else ScopedDeepSeekConfiguration.objects.filter(
            workspace=workspace
        ).first()
    )
    return ScopedDeepSeekPublicStatus(
        configured=bool(
            configuration is not None
            and configuration.encrypted_api_key
            and configuration.encryption_scheme
        ),
        masked_key="" if configuration is None else configuration.masked_key,
        endpoint=DEEPSEEK_BASE_URL,
        model_name=(
            "deepseek-v4-flash"
            if configuration is None
            else configuration.model_name
        ),
        thinking_enabled=(
            False if configuration is None else configuration.thinking_enabled
        ),
        api_revision=0 if workspace is None else workspace.api_revision,
    )


def resolve_scoped_deepseek_settings(
    course_id: str,
    class_id: str,
) -> ScopedDeepSeekSettings | None:
    status = scoped_deepseek_status(course_id, class_id)
    if not status.configured:
        return None
    configuration = ScopedDeepSeekConfiguration.objects.select_related(
        "workspace"
    ).get(
        workspace__course_id=course_id,
        workspace__class_id=class_id,
    )
    try:
        api_key = unprotect_secret(
            ProtectedSecret(
                scheme=configuration.encryption_scheme,
                payload=bytes(configuration.encrypted_api_key),
            ),
            context=_context(course_id, class_id),
        )
    except ValueError:
        return None
    return ScopedDeepSeekSettings(
        api_key=api_key,
        model_name=status.model_name,
        thinking_enabled=status.thinking_enabled,
        api_revision=status.api_revision,
    )


def save_scoped_deepseek_settings(
    *,
    course_id: str,
    class_id: str,
    api_key: str | None,
    model_name: str,
    thinking_enabled: bool,
    updated_by,
    clear_key: bool = False,
) -> ScopedDeepSeekPublicStatus:
    _validate_scope(course_id, class_id)
    if model_name not in SUPPORTED_DEEPSEEK_MODELS:
        raise ValueError("unsupported DeepSeek model")
    if type(thinking_enabled) is not bool:
        raise ValueError("DeepSeek thinking mode must be boolean")
    cleaned = None if api_key is None else api_key.strip()
    if cleaned is not None and (
        not cleaned
        or len(cleaned) > _MAX_KEY_LENGTH
        or any(character.isspace() for character in cleaned)
    ):
        raise ValueError("DeepSeek API key is invalid")

    with transaction.atomic():
        workspace, _ = CourseClassWorkspace.objects.get_or_create(
            course_id=course_id,
            class_id=class_id,
        )
        workspace = CourseClassWorkspace.objects.select_for_update().get(
            pk=workspace.pk
        )
        configuration, _ = ScopedDeepSeekConfiguration.objects.get_or_create(
            workspace=workspace
        )
        if clear_key:
            configuration.encrypted_api_key = None
            configuration.encryption_scheme = ""
            configuration.masked_key = ""
        elif cleaned is not None:
            protected = protect_secret(
                cleaned,
                context=_context(course_id, class_id),
            )
            configuration.encrypted_api_key = protected.payload
            configuration.encryption_scheme = protected.scheme
            configuration.masked_key = _mask(cleaned)
        configuration.model_name = model_name
        configuration.thinking_enabled = thinking_enabled
        configuration.updated_by = updated_by
        configuration.save()
        workspace.api_revision += 1
        workspace.save(update_fields=("api_revision", "updated_at"))
    return scoped_deepseek_status(course_id, class_id)


def _validate_scope(course_id: str, class_id: str) -> None:
    if not _scope_pattern.fullmatch(course_id) or not _scope_pattern.fullmatch(
        class_id
    ):
        raise ValueError("scope identifier is invalid")


def _context(course_id: str, class_id: str) -> str:
    return f"{course_id}\0{class_id}"


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


__all__ = [
    "ScopedDeepSeekPublicStatus",
    "ScopedDeepSeekSettings",
    "resolve_scoped_deepseek_settings",
    "save_scoped_deepseek_settings",
    "scoped_deepseek_status",
]
