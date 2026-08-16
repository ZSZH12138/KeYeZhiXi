"""Non-sensitive production capability readiness checks."""

from __future__ import annotations

from collections.abc import Mapping

from course_insight.application.persistence import backend_readiness
from course_insight.modules.m1_course_governance.parser_protocol import (
    ParserRegistry,
)


_REQUIRED_PARSER_EXTENSIONS = frozenset(
    {".md", ".txt", ".pdf", ".docx", ".pptx"}
)
_READINESS_KEYS = (
    "status",
    "backend",
    "parser",
    "embedding",
    "vector_store",
    "retrieval_audit",
    "teacher_review",
)


def evaluate_capability_readiness(
    *,
    backend: object,
    parser_registry: object,
    m2_service: object,
    m3_service: object,
    production: bool,
) -> dict[str, str]:
    """Return a safe readiness snapshot for the configured S1-S4 ports.

    The check is deliberately dependency-oriented: it proves that the selected
    backend and required ports are bound, but does not perform network calls or
    fabricate a ready index.  Non-production callers may use the same shape,
    while production callers receive ``unavailable`` for any missing port.
    """

    backend_status = backend_readiness(backend)
    parser_ok = _parser_ready(parser_registry)
    embedding_ok = _bound(m2_service, "_embedding_provider")
    vector_ok = _bound(m2_service, "_vector_store")
    audit_ok = _bound(m2_service, "_audit_store")
    review_ok = _review_ready(m3_service)

    result = {
        "status": "unavailable",
        "backend": "ok" if backend_status.ready else "unavailable",
        "parser": "ok" if parser_ok else "unavailable",
        "embedding": "ok" if embedding_ok else "unavailable",
        "vector_store": "ok" if vector_ok else "unavailable",
        "retrieval_audit": "ok" if audit_ok else "unavailable",
        "teacher_review": "ok" if review_ok else "unavailable",
    }
    if not production:
        result["status"] = "ready"
        return result
    if all(result[key] == "ok" for key in _READINESS_KEYS[1:]):
        result["status"] = "ready"
    return result


def _parser_ready(value: object) -> bool:
    if not isinstance(value, ParserRegistry):
        return False
    try:
        entries = getattr(value, "_entries")
        return isinstance(entries, Mapping) and _REQUIRED_PARSER_EXTENSIONS <= set(entries)
    except (AttributeError, TypeError, ValueError):
        return False


def _bound(value: object, attribute: str) -> bool:
    try:
        return getattr(value, attribute) is not None
    except (AttributeError, TypeError):
        return False


def _review_ready(value: object) -> bool:
    try:
        return (
            bool(getattr(value, "_require_teacher_approval"))
            and getattr(value, "_review_workflow") is not None
        )
    except (AttributeError, TypeError):
        return False


__all__ = ["evaluate_capability_readiness"]
