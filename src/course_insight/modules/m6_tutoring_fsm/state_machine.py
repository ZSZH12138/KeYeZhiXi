"""Immutable S0-S5 transition policy for deterministic tutoring control."""

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
    """Choose one auditable next state from explicit evidence conditions."""

    def choose_next(
        self,
        current_state: str,
        *,
        needs_review: bool,
        has_misconception: bool,
    ) -> str:
        """Select S2 for remediation and otherwise advance safely."""

        if current_state == "S1":
            candidate = "S2" if needs_review or has_misconception else "S3"
        elif current_state == "S4" and (needs_review or has_misconception):
            candidate = "S2"
        else:
            defaults = {"S0": "S1", "S2": "S3", "S3": "S4", "S4": "S5"}
            candidate = defaults.get(current_state, "")
        if candidate not in ALLOWED_TRANSITIONS.get(current_state, frozenset()):
            raise DomainError(
                code="INVALID_STATE_TRANSITION",
                module="m6",
                message="no safe tutoring transition is available",
                details={"current_state": current_state},
                recoverable=True,
            )
        return candidate


DEFAULT_STATE_MACHINE = DefaultTutoringStateMachine()
