"""Cross-module coordinator for physical pseudonymous actor erasure."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Protocol


_ACTOR = re.compile(r"^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$")
_ORDER = ("m9", "m8", "m7", "m6", "m5", "m4", "m0")


class ActorPurgePort(Protocol):
    def purge_actor(self, actor_id: str) -> int:
        """Physically delete module-owned data for one actor."""


@dataclass(frozen=True, slots=True)
class ActorErasureResult:
    module_counts: tuple[tuple[str, int], ...]

    @property
    def total_deleted(self) -> int:
        return sum(count for _, count in self.module_counts)


class ActorErasureCoordinator:
    """Invoke every person-bearing module purge port in dependency order."""

    def __init__(self, repositories: Mapping[str, ActorPurgePort]) -> None:
        missing = set(_ORDER) - set(repositories)
        if missing:
            raise ValueError("actor erasure repositories are incomplete")
        self._repositories = {
            name: repositories[name] for name in _ORDER
        }

    @classmethod
    def from_container(cls, container: object) -> "ActorErasureCoordinator":
        services = {
            "m0": getattr(container, "m0_service"),
            "m4": getattr(container, "m4_service"),
            "m5": getattr(container, "m5_service"),
            "m6": getattr(container, "m6_service"),
            "m7": getattr(container, "m7_service"),
            "m8": getattr(container, "m8_service"),
            "m9": getattr(container, "m9_service"),
        }
        repositories = {
            "m0": getattr(services["m0"], "_repository"),
            "m4": getattr(services["m4"], "_repository"),
            "m5": getattr(services["m5"], "_repository"),
            "m6": getattr(services["m6"], "_repository"),
            "m7": getattr(services["m7"], "_prompt_repository"),
            "m8": getattr(services["m8"], "_repository"),
            "m9": getattr(services["m9"], "_repository"),
        }
        return cls(repositories)

    def purge(self, actor_id: str) -> ActorErasureResult:
        if not isinstance(actor_id, str) or not _ACTOR.fullmatch(actor_id):
            raise ValueError("actor_id is invalid")
        counts: list[tuple[str, int]] = []
        for module, repository in self._repositories.items():
            purge = getattr(repository, "purge_actor", None)
            if not callable(purge):
                raise RuntimeError(f"{module} actor erasure is unavailable")
            count = purge(actor_id)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise RuntimeError(f"{module} actor erasure result is invalid")
            counts.append((module, count))
        return ActorErasureResult(module_counts=tuple(counts))


__all__ = ["ActorErasureCoordinator", "ActorErasureResult", "ActorPurgePort"]
