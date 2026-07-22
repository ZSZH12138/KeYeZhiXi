"""The single authoritative S0-S5 tutoring transition graph."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from course_insight.contracts.errors import DomainError


ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "S0": frozenset({"S1"}),
        "S1": frozenset({"S2", "S3"}),
        "S2": frozenset({"S3"}),
        "S3": frozenset({"S4"}),
        "S4": frozenset({"S2", "S3", "S5"}),
        "S5": frozenset(),
    }
)


class DefaultTutoringStateMachine:
    """Validate candidate transitions against the authoritative graph."""

    def validate_transition(self, current_state: str, next_state: str) -> None:
        """Accept one legal edge and reject every other state pair uniformly."""

        if next_state not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
            raise DomainError(
                code="INVALID_STATE_TRANSITION",
                module="m6",
                message="the requested tutoring state transition is not allowed",
                details={
                    "current_state": current_state,
                    "next_state": next_state,
                },
            )


DEFAULT_STATE_MACHINE = DefaultTutoringStateMachine()
