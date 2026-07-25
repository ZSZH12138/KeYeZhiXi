"""Stable PostgreSQL infrastructure errors."""

from __future__ import annotations


class PostgresError(RuntimeError):
    """Base class for safe PostgreSQL infrastructure failures."""


class PostgresConnectionError(PostgresError):
    """A connection or pool is unavailable."""


class PostgresOperationError(PostgresError):
    """A repository operation failed without a domain-specific mapping."""


class PostgresMigrationError(PostgresError):
    """The PostgreSQL migration set or applied state is invalid."""


__all__ = [
    "PostgresConnectionError",
    "PostgresError",
    "PostgresMigrationError",
    "PostgresOperationError",
]
