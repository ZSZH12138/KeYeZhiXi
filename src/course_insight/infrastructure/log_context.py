"""Immutable correlation context for application logs."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator


_CONTEXT_FIELDS = (
    "run_id",
    "request_id",
    "actor_id",
    "course_id",
    "class_id",
    "attempt_id",
    "job_id",
    "worker_id",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


@dataclass(frozen=True, slots=True)
class LogContext:
    """Safe identifiers shared by one Web request, job, or worker action."""

    run_id: str | None = None
    request_id: str | None = None
    actor_id: str | None = None
    course_id: str | None = None
    class_id: str | None = None
    attempt_id: str | None = None
    job_id: str | None = None
    worker_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Return a fresh mapping so callers cannot mutate shared context."""

        return {
            field: getattr(self, field)
            for field in _CONTEXT_FIELDS
        }


_EMPTY_CONTEXT = LogContext()
_LOG_CONTEXT: ContextVar[LogContext] = ContextVar(
    "course_insight_log_context",
    default=_EMPTY_CONTEXT,
)


def current_log_context() -> LogContext:
    """Return the current task/thread-local immutable context."""

    return _LOG_CONTEXT.get()


def _validate_context_value(field: str, value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a safe identifier")
    return value


@contextmanager
def bind_log_context(**values: str | None) -> Iterator[LogContext]:
    """Temporarily overlay correlation fields and restore them on exit."""

    unknown = set(values) - set(_CONTEXT_FIELDS)
    if unknown:
        raise ValueError("unknown log context field")

    validated = {
        field: _validate_context_value(field, value)
        for field, value in values.items()
    }
    next_context = replace(current_log_context(), **validated)
    token = _LOG_CONTEXT.set(next_context)
    try:
        yield next_context
    finally:
        _LOG_CONTEXT.reset(token)


__all__ = [
    "LogContext",
    "bind_log_context",
    "current_log_context",
]
