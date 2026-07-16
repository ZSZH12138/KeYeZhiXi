"""M0 platform module."""

from course_insight.modules.m0_platform.repository import M0Repository
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m0_platform.stubs import M0PlatformServiceStub

__all__ = ["M0PlatformService", "M0PlatformServiceStub", "M0Repository"]
