"""Shared fail-closed guard for live PostgreSQL integration tests."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

import pytest


TEST_DATABASE_URL_ENV = "COURSE_INSIGHT_TEST_DATABASE_URL"
TEST_DATABASE_NAME_ENV = "COURSE_INSIGHT_TEST_DATABASE_NAME"
_DISPOSABLE_NAME = re.compile(
    r"(?:^|[_-])(?:test|ci|tmp)(?:$|[_-])",
    re.IGNORECASE,
)
_RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})


def require_live_test_database_url() -> str:
    """Return one explicitly confirmed disposable database DSN or skip/fail."""

    dsn = os.environ.get(TEST_DATABASE_URL_ENV)
    if not dsn:
        pytest.skip(
            f"{TEST_DATABASE_URL_ENV} is unset; "
            "real PostgreSQL integration was not run"
        )
    confirmed_name = os.environ.get(TEST_DATABASE_NAME_ENV)
    if not confirmed_name:
        pytest.skip(
            f"{TEST_DATABASE_NAME_ENV} is unset; "
            "destructive PostgreSQL integration was not run"
        )
    database_name = _database_name_from_dsn(dsn)
    if database_name is None:
        pytest.fail(
            "destructive PostgreSQL integration requires a DSN with a "
            "concrete database name",
            pytrace=False,
        )
    if confirmed_name != database_name:
        pytest.fail(
            f"{TEST_DATABASE_NAME_ENV} does not match the configured "
            "PostgreSQL database name",
            pytrace=False,
        )
    lowered = database_name.lower()
    if lowered in _RESERVED_DATABASES:
        pytest.fail(
            "destructive PostgreSQL integration cannot target a reserved "
            "PostgreSQL database",
            pytrace=False,
        )
    if _DISPOSABLE_NAME.search(lowered) is None:
        pytest.fail(
            "destructive PostgreSQL integration database names must contain "
            "at least one of: test, ci, tmp",
            pytrace=False,
        )
    return dsn


def _database_name_from_dsn(dsn: str) -> str | None:
    parsed = urlparse(dsn)
    if parsed.scheme in {"postgres", "postgresql"}:
        path = parsed.path.lstrip("/")
        if path:
            return path
        query_database = parse_qs(parsed.query).get("dbname")
        if query_database:
            return query_database[0]
        return None
    if "dbname=" not in dsn:
        return None
    for fragment in dsn.split():
        if fragment.startswith("dbname="):
            value = fragment.partition("=")[2]
            return value or None
    return None


__all__ = [
    "TEST_DATABASE_NAME_ENV",
    "TEST_DATABASE_URL_ENV",
    "require_live_test_database_url",
]
