"""M0 platform module."""

from __future__ import annotations

from course_insight.modules.m0_platform.repository import M0Repository

__all__ = ["M0PlatformService", "M0PlatformServiceStub", "M0Repository"]


def __getattr__(name: str):
    if name == "M0PlatformService":
        from course_insight.modules.m0_platform.service import M0PlatformService

        return M0PlatformService
    if name == "M0PlatformServiceStub":
        from course_insight.modules.m0_platform.stubs import (
            M0PlatformServiceStub,
        )

        return M0PlatformServiceStub
    raise AttributeError(name)
