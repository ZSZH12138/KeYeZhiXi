"""A small Psycopg pool boundary with dict rows and safe failures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from course_insight.infrastructure.postgresql.base import (
    PostgresConnectionError,
)
from course_insight.infrastructure.postgresql.connection import (
    PostgresConnection,
    libpq_connect_timeout,
    validated_timeout_seconds,
)


_CHECK_CONNECTION = ConnectionPool.check_connection
_CONNECTION_ERROR_MESSAGE = "PostgreSQL connection is unavailable"
_POOL_ERRORS = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    PoolClosed,
    PoolTimeout,
)


class PostgresPool:
    """Own one ready-or-explicitly-closed Psycopg connection pool."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout_seconds: float = 10.0,
        open: bool = True,
    ) -> None:
        if type(dsn) is not str or not dsn.strip() or "\x00" in dsn:
            raise ValueError("PostgreSQL DSN must be a non-empty string")
        _validate_pool_sizes(min_size, max_size)
        wait_timeout = validated_timeout_seconds(
            connect_timeout_seconds,
            field="connect timeout",
        )
        self._wait_timeout = wait_timeout
        self._driver_pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            kwargs={
                "connect_timeout": libpq_connect_timeout(wait_timeout),
                "row_factory": dict_row,
            },
            open=False,
            timeout=wait_timeout,
            check=_CHECK_CONNECTION,
        )
        self._opened = False
        self._closed = False
        if open:
            self.open()

    def __repr__(self) -> str:
        state = (
            "closed"
            if self._closed
            else "open"
            if self._opened
            else "not-opened"
        )
        return f"{type(self).__name__}(state={state!r})"

    def __enter__(self) -> "PostgresPool":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def open(self, *, timeout: float | None = None) -> None:
        """Open the pool synchronously and fail without echoing its DSN."""

        if self._closed:
            raise PostgresConnectionError(_CONNECTION_ERROR_MESSAGE)
        if self._opened:
            return
        wait_timeout = (
            self._wait_timeout
            if timeout is None
            else validated_timeout_seconds(timeout, field="pool open timeout")
        )
        try:
            self._driver_pool.open(wait=True, timeout=wait_timeout)
        except _POOL_ERRORS:
            raise PostgresConnectionError(_CONNECTION_ERROR_MESSAGE) from None
        self._opened = True

    @contextmanager
    def connection(
        self,
        *,
        timeout: float | None = None,
    ) -> Iterator[PostgresConnection]:
        """Yield a dict-row connection and sanitize availability failures."""

        request_timeout = (
            None
            if timeout is None
            else validated_timeout_seconds(
                timeout,
                field="pool connection timeout",
            )
        )
        try:
            with self._driver_pool.connection(
                timeout=request_timeout
            ) as connection:
                yield connection
        except _POOL_ERRORS:
            raise PostgresConnectionError(_CONNECTION_ERROR_MESSAGE) from None

    def close(self) -> None:
        """Close once; subsequent calls are harmless."""

        if self._closed:
            return
        try:
            self._driver_pool.close()
        except _POOL_ERRORS:
            raise PostgresConnectionError(_CONNECTION_ERROR_MESSAGE) from None
        finally:
            self._opened = False
            self._closed = True


def create_postgres_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    connect_timeout_seconds: float = 10.0,
    open: bool = True,
) -> PostgresPool:
    """Create the shared PostgreSQL pool used by repositories and workers."""

    return PostgresPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        connect_timeout_seconds=connect_timeout_seconds,
        open=open,
    )


def _validate_pool_sizes(min_size: int, max_size: int) -> None:
    if (
        type(min_size) is not int
        or type(max_size) is not int
        or min_size < 1
        or max_size < min_size
    ):
        raise ValueError("pool size must be positive and ordered")


__all__ = ["PostgresPool", "create_postgres_pool"]
