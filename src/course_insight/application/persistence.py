"""Application-level persistence selection and readiness ports.

This module intentionally knows nothing about SQLite, PostgreSQL, Django, or
the application factory.  Concrete adapters are supplied by callers through
factories, which keeps backend choice explicit and prevents an unavailable
authoritative backend from silently falling back to another one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol, runtime_checkable


BackendName = Literal["sqlite", "postgresql"]


class PersistenceConfigurationError(ValueError):
    """Safe configuration/selection failure with a stable machine code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BackendReadiness:
    """Non-sensitive readiness result for one selected backend."""

    backend: str
    ready: bool
    reason: str | None = None


@runtime_checkable
class PersistenceBackend(Protocol):
    """Minimal contract implemented by an authoritative persistence adapter."""

    @property
    def backend_name(self) -> str:
        """Return the configured backend identity."""

    def initialize(self) -> None:
        """Initialize or migrate the selected backend."""

    def is_ready(self) -> bool:
        """Return whether the selected backend is usable."""


BackendFactory = Callable[[], object]


@dataclass(frozen=True, slots=True)
class PersistenceBackendSelector:
    """Select exactly one backend from caller-supplied factories."""

    backend: str
    sqlite_factory: BackendFactory | None
    postgresql_factory: BackendFactory | None

    def select(self) -> object:
        """Construct only the configured backend; never perform fallback."""

        if self.backend not in {"sqlite", "postgresql"}:
            raise PersistenceConfigurationError(
                code="PERSISTENCE_BACKEND_INVALID",
                message="configured persistence backend is invalid",
            )
        factory = (
            self.sqlite_factory
            if self.backend == "sqlite"
            else self.postgresql_factory
        )
        if factory is None:
            raise PersistenceConfigurationError(
                code="PERSISTENCE_BACKEND_UNAVAILABLE",
                message="configured persistence backend is unavailable",
            )
        selected = factory()
        if selected is None:
            raise PersistenceConfigurationError(
                code="PERSISTENCE_BACKEND_UNAVAILABLE",
                message="configured persistence backend is unavailable",
            )
        selected_name = getattr(selected, "backend_name", None)
        if selected_name is not None and selected_name != self.backend:
            raise PersistenceConfigurationError(
                code="PERSISTENCE_BACKEND_MISMATCH",
                message="selected persistence adapter does not match configuration",
            )
        return selected


def backend_readiness(backend: object) -> BackendReadiness:
    """Normalize an adapter readiness check without exposing implementation data."""

    name = getattr(backend, "backend_name", "unknown")
    if not isinstance(name, str) or not name.strip():
        name = "unknown"
    try:
        method = getattr(backend, "readiness", None)
        if callable(method):
            result = method()
            if isinstance(result, BackendReadiness):
                return result
            if isinstance(result, bool):
                return BackendReadiness(
                    backend=name,
                    ready=result,
                    reason=None if result else "backend_unavailable",
                )
        method = getattr(backend, "is_ready", None)
        if callable(method):
            result = method()
            if isinstance(result, bool):
                return BackendReadiness(
                    backend=name,
                    ready=result,
                    reason=None if result else "backend_unavailable",
                )
    except Exception:
        return BackendReadiness(backend=name, ready=False, reason="backend_unavailable")
    return BackendReadiness(backend=name, ready=False, reason="readiness_unavailable")


def require_backend_ready(backend: object) -> object:
    """Return an adapter only when its readiness contract is affirmative."""

    status = backend_readiness(backend)
    if not status.ready:
        raise PersistenceConfigurationError(
            code="PERSISTENCE_BACKEND_UNAVAILABLE",
            message="authoritative persistence backend is not ready",
        )
    return backend


__all__ = [
    "BackendName",
    "BackendReadiness",
    "PersistenceBackend",
    "PersistenceBackendSelector",
    "PersistenceConfigurationError",
    "backend_readiness",
    "require_backend_ready",
]
