from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """模块边界统一业务异常。"""

    def __init__(
        self,
        *,
        code: str,
        module: str,
        message: str,
        details: dict[str, Any] | None = None,
        recoverable: bool = False,
    ) -> None:
        self.code = code
        self.module = module
        self.message = message
        self.details = dict(details or {})
        self.recoverable = recoverable
        super().__init__(self.__str__())

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "module": self.module,
            "message": self.message,
            "details": dict(self.details),
            "recoverable": self.recoverable,
        }

    def __str__(self) -> str:
        return f"[{self.module}:{self.code}] {self.message}"

    def with_detail(self, key: str, value: Any) -> DomainError:
        return DomainError(
            code=self.code,
            module=self.module,
            message=self.message,
            details={**self.details, key: value},
            recoverable=self.recoverable,
        )
