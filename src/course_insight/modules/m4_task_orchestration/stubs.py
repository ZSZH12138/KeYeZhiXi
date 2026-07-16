"""Deterministic zero-argument M4 service stub."""

from typing import cast

from course_insight.modules.m4_task_orchestration.repository import M4Repository
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)


class M4TaskOrchestrationServiceStub(M4TaskOrchestrationService):
    """Instantiate M4 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(cast(M4Repository, object()), object())
