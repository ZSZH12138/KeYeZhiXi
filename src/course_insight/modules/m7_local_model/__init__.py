"""M7 local-model module."""

from course_insight.modules.m7_local_model.repository import M7Repository
from course_insight.modules.m7_local_model.service import M7LocalModelService
from course_insight.modules.m7_local_model.stubs import M7LocalModelServiceStub

__all__ = ["M7LocalModelService", "M7LocalModelServiceStub", "M7Repository"]
