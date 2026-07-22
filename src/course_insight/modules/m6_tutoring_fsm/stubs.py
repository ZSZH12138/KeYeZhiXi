"""Deterministic zero-argument M6 service stub."""

from course_insight.modules.m6_tutoring_fsm.repository import InMemoryM6Repository
from course_insight.modules.m6_tutoring_fsm.service import M6TutoringControlService
from course_insight.modules.m6_tutoring_fsm.state_machine import DEFAULT_STATE_MACHINE


class M6TutoringControlServiceStub(M6TutoringControlService):
    """Instantiate M6 with a fixed local in-memory repository."""

    def __init__(self) -> None:
        super().__init__(DEFAULT_STATE_MACHINE, InMemoryM6Repository())
