"""Deterministic zero-argument M8 service stub."""

from typing import cast

from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from course_insight.modules.m8_assessment_scoring.repository import M8Repository
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService


class M8AssessmentServiceStub(M8AssessmentService):
    """Instantiate M8 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        super().__init__(
            cast(M8Repository, object()),
            RuleScorer(),
            PaperGenerator(),
        )
