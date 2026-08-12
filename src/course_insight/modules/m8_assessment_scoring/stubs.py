"""Deterministic zero-argument M8 service stub."""

from datetime import datetime, timedelta, timezone

from typing import cast

from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from course_insight.modules.m8_assessment_scoring.repository import M8Repository
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService


FIXED_TIME = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class FixedClock:
    """Explicit deterministic clock reserved for tests and local stubs."""

    def __init__(self, value: datetime = FIXED_TIME) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fixed clock requires a timezone-aware value")
        self._value = value

    def now(self) -> datetime:
        return self._value


class M8AssessmentServiceStub(M8AssessmentService):
    """Instantiate M8 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        clock = FixedClock()
        super().__init__(
            cast(M8Repository, object()),
            RuleScorer(clock),
            PaperGenerator(clock),
            clock,
        )


__all__ = ["FIXED_TIME", "FixedClock", "M8AssessmentServiceStub"]
