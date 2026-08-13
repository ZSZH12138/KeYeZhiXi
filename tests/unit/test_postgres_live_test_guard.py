from __future__ import annotations

import pytest
from _pytest.outcomes import Failed

from tests.integration._postgres_live import (
    TEST_DATABASE_NAME_ENV,
    TEST_DATABASE_URL_ENV,
    require_live_test_database_url,
)


def test_live_guard_skips_without_confirmation_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        TEST_DATABASE_URL_ENV,
        "postgresql://tester:secret@db.internal/course_insight_test_ci",
    )
    monkeypatch.delenv(TEST_DATABASE_NAME_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception, match=TEST_DATABASE_NAME_ENV):
        require_live_test_database_url()


def test_live_guard_fails_when_confirmation_mismatches_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        TEST_DATABASE_URL_ENV,
        "postgresql://tester:secret@db.internal/course_insight_test_ci",
    )
    monkeypatch.setenv(TEST_DATABASE_NAME_ENV, "different_test_ci")

    with pytest.raises(Failed, match="does not match"):
        require_live_test_database_url()


@pytest.mark.parametrize("database_name", ["postgres", "template0", "template1"])
def test_live_guard_fails_for_reserved_database_names(
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
) -> None:
    monkeypatch.setenv(
        TEST_DATABASE_URL_ENV,
        f"postgresql://tester:secret@db.internal/{database_name}",
    )
    monkeypatch.setenv(TEST_DATABASE_NAME_ENV, database_name)

    with pytest.raises(Failed, match="reserved PostgreSQL database"):
        require_live_test_database_url()


def test_live_guard_fails_when_database_name_is_not_marked_disposable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        TEST_DATABASE_URL_ENV,
        "postgresql://tester:secret@db.internal/course_insight_prod",
    )
    monkeypatch.setenv(TEST_DATABASE_NAME_ENV, "course_insight_prod")

    with pytest.raises(Failed, match="must contain at least one of"):
        require_live_test_database_url()


@pytest.mark.parametrize(
    "database_name",
    ["social_prod", "critical_prod", "latest_production"],
)
def test_live_guard_requires_a_delimited_disposable_marker(
    monkeypatch: pytest.MonkeyPatch,
    database_name: str,
) -> None:
    monkeypatch.setenv(
        TEST_DATABASE_URL_ENV,
        f"postgresql://tester:secret@db.internal/{database_name}",
    )
    monkeypatch.setenv(TEST_DATABASE_NAME_ENV, database_name)

    with pytest.raises(Failed, match="must contain at least one of"):
        require_live_test_database_url()


def test_live_guard_accepts_confirmed_disposable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = "postgresql://tester:secret@db.internal/course_insight_test_ci"
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, dsn)
    monkeypatch.setenv(
        TEST_DATABASE_NAME_ENV,
        "course_insight_test_ci",
    )

    assert require_live_test_database_url() == dsn
