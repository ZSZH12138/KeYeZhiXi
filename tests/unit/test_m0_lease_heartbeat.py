from __future__ import annotations

from contextvars import ContextVar
from threading import Event

import pytest

from course_insight.modules.m0_platform.lease_heartbeat import (
    run_with_lease_heartbeat,
)


def test_heartbeat_renews_while_action_is_running() -> None:
    renewals: list[int] = []
    release = Event()

    def action() -> str:
        assert release.wait(timeout=1)
        return "saved"

    def renew() -> bool:
        renewals.append(1)
        if len(renewals) >= 2:
            release.set()
        return True

    outcome = run_with_lease_heartbeat(
        action,
        renew=renew,
        heartbeat_interval_seconds=0.005,
    )

    assert outcome.value == "saved"
    assert outcome.error is None
    assert outcome.lease_current is True
    assert len(renewals) >= 2


def test_heartbeat_reports_lost_lease_without_discarding_action_error() -> None:
    expected = RuntimeError("callback failed")
    renewals: list[int] = []
    release = Event()

    def action() -> None:
        assert release.wait(timeout=1)
        raise expected

    def renew() -> bool:
        renewals.append(1)
        if len(renewals) >= 2:
            release.set()
        return len(renewals) < 2

    outcome = run_with_lease_heartbeat(
        action,
        renew=renew,
        heartbeat_interval_seconds=0.005,
    )

    assert outcome.value is None
    assert outcome.error is expected
    assert outcome.lease_current is False


def test_heartbeat_propagates_request_context_to_action_thread() -> None:
    request_id: ContextVar[str | None] = ContextVar(
        "test_request_id",
        default=None,
    )
    request_id.set("request_123")

    outcome = run_with_lease_heartbeat(
        request_id.get,
        renew=lambda: True,
        heartbeat_interval_seconds=0.01,
    )

    assert outcome.value == "request_123"


@pytest.mark.parametrize("interval", [0.0, -1.0, float("nan"), float("inf")])
def test_heartbeat_rejects_invalid_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="heartbeat interval"):
        run_with_lease_heartbeat(
            lambda: None,
            renew=lambda: True,
            heartbeat_interval_seconds=interval,
        )
