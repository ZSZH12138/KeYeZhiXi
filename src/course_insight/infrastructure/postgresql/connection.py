"""Psycopg connection policy with stable, redacted failures."""

from __future__ import annotations

import math
from typing import Any, TypeAlias, cast

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow, dict_row

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
)


PostgresRow: TypeAlias = DictRow
PostgresConnection: TypeAlias = Connection[PostgresRow]

_CONNECTION_ERROR_MESSAGE = "PostgreSQL connection is unavailable"


def validated_timeout_seconds(value: float, *, field: str) -> float:
    """Return one finite positive timeout without accepting booleans."""

    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a finite positive number")
    return float(value)


def libpq_connect_timeout(value: float) -> int:
    """Convert an application timeout to libpq's whole-second setting."""

    validated = validated_timeout_seconds(value, field="connect timeout")
    return max(1, math.ceil(validated))


def connect_postgres(
    dsn: str,
    *,
    connect_timeout_seconds: float = 10.0,
) -> PostgresConnection:
    """Open one dict-row Psycopg connection with redacted failures."""

    if type(dsn) is not str or not dsn.strip() or "\x00" in dsn:
        raise ValueError("PostgreSQL DSN must be a non-empty string")
    timeout = libpq_connect_timeout(connect_timeout_seconds)
    try:
        connection = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=timeout,
        )
    except psycopg.Error:
        raise PostgresConnectionError(_CONNECTION_ERROR_MESSAGE) from None
    return cast(PostgresConnection, connection)


__all__ = [
    "PostgresConnection",
    "PostgresConnectionError",
    "PostgresRow",
    "connect_postgres",
    "libpq_connect_timeout",
    "validated_timeout_seconds",
]
