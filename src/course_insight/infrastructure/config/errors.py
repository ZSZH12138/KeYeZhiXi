"""Safe internal errors for platform configuration and role seeds."""

from __future__ import annotations

from collections.abc import Iterable


class ConfigurationError(RuntimeError):
    """Configuration failure that never renders rejected values."""

    def __init__(
        self,
        *,
        code: str,
        fields: Iterable[str],
        reason: str = "invalid",
    ) -> None:
        self.code = code
        self.fields = tuple(sorted(set(fields))) or ("configuration",)
        self.reason = reason
        super().__init__(self.__str__())

    def __str__(self) -> str:
        joined_fields = ", ".join(self.fields)
        return (
            f"[configuration:{self.code}] {self.reason}: "
            f"{joined_fields}"
        )


class RoleSeedError(ConfigurationError):
    """A malformed or conflicting role seed."""

    def __init__(self, *, fields: Iterable[str], reason: str) -> None:
        super().__init__(
            code="INVALID_ROLE_SEED",
            fields=fields,
            reason=reason,
        )
