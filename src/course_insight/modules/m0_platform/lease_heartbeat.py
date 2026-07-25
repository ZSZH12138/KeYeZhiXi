"""Small fail-closed heartbeat runner for transaction-free leased work."""

from __future__ import annotations

import math
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass
from queue import Queue
from threading import Event, Thread
from typing import Generic, TypeVar, cast


ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True)
class LeaseHeartbeatOutcome(Generic[ResultT]):
    """The action result together with the final lease-ownership decision."""

    value: ResultT | None
    error: BaseException | None
    lease_current: bool


def run_with_lease_heartbeat(
    action: Callable[[], ResultT],
    *,
    renew: Callable[[], bool],
    heartbeat_interval_seconds: float,
) -> LeaseHeartbeatOutcome[ResultT]:
    """Run blocking work off-thread while the caller renews its lease.

    A Python thread cannot safely cancel arbitrary work. If renewal fails, this
    function waits for the action to finish but marks its value as stale so the
    caller can refuse every subsequent state transition.
    """

    interval = _validated_interval(heartbeat_interval_seconds)
    completed = Event()
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)
    action_context = copy_context()

    def invoke() -> None:
        try:
            result.put((True, action()))
        except BaseException as error:
            result.put((False, error))
        finally:
            completed.set()

    Thread(
        target=lambda: action_context.run(invoke),
        name="m0-lease-heartbeat-action",
        daemon=True,
    ).start()
    lease_current = True
    while not completed.wait(interval):
        if not lease_current:
            continue
        try:
            lease_current = renew() is True
        except Exception:
            lease_current = False

    succeeded, payload = result.get()
    if succeeded:
        return LeaseHeartbeatOutcome(
            value=cast(ResultT, payload),
            error=None,
            lease_current=lease_current,
        )
    return LeaseHeartbeatOutcome(
        value=None,
        error=cast(BaseException, payload),
        lease_current=lease_current,
    )


def _validated_interval(value: float) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value <= 0.0
    ):
        raise ValueError("heartbeat interval must be finite and positive")
    return float(value)
