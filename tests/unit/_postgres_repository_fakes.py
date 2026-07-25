"""Small Psycopg-shaped test doubles for PostgreSQL repository unit tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


Row = dict[str, Any]
Response = Row | list[Row] | None
Responder = Callable[[str, tuple[Any, ...]], Response]


class FakeCursor:
    def __init__(self, response: Response) -> None:
        if response is None:
            self._rows: list[Row] = []
        elif isinstance(response, list):
            self._rows = response
        else:
            self._rows = [response]

    def fetchone(self) -> Row | None:
        return None if not self._rows else self._rows[0]

    def fetchall(self) -> list[Row]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.transaction_entries = 0

    def execute(
        self,
        statement: str,
        parameters: tuple[Any, ...] = (),
    ) -> FakeCursor:
        normalized_parameters = tuple(parameters)
        self.executions.append((statement, normalized_parameters))
        return FakeCursor(self._responder(statement, normalized_parameters))

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.transaction_entries += 1
        yield


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self.connection_entries = 0

    @contextmanager
    def connection(self) -> Iterator[FakeConnection]:
        self.connection_entries += 1
        yield self._connection


class FailingPool:
    def __init__(self, error: Exception) -> None:
        self._error = error

    @contextmanager
    def connection(self) -> Iterator[FakeConnection]:
        raise self._error
        yield  # pragma: no cover
