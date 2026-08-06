"""Deterministic zero-argument M8 service stub."""

from course_insight.contracts.assessment import AssessmentPaper, ScoreAuditRecord
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import (
    FixedClock,
    M8AssessmentService,
)


class InMemoryM8Repository:
    """In-process M8 repository for tests and deterministic integration."""

    def __init__(self) -> None:
        self._papers: dict[str, AssessmentPaper] = {}
        self._audits: dict[tuple[str, int], ScoreAuditRecord] = {}

    def save_paper(self, paper: AssessmentPaper) -> None:
        self._papers[paper.paper_id] = paper.model_copy(deep=True)

    def get_paper(self, paper_id: str) -> AssessmentPaper | None:
        record = self._papers.get(paper_id)
        return record.model_copy(deep=True) if record is not None else None

    def save_score_audit(self, record: ScoreAuditRecord) -> None:
        self._audits[
            (record.audit_id, record.audit_version)
        ] = record.model_copy(deep=True)

    def get_score_audit(
        self,
        audit_id: str,
        audit_version: int,
    ) -> ScoreAuditRecord | None:
        record = self._audits.get((audit_id, audit_version))
        return record.model_copy(deep=True) if record is not None else None

    def get_latest_score_audit(self, audit_id: str) -> ScoreAuditRecord | None:
        candidates = [
            rec for (aid, _ver), rec in self._audits.items() if aid == audit_id
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda r: r.audit_version)
        return latest.model_copy(deep=True)


class M8AssessmentServiceStub(M8AssessmentService):
    """Instantiate M8 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        clock = FixedClock()
        super().__init__(
            InMemoryM8Repository(),
            RuleScorer(),
            PaperGenerator(clock=clock),
            clock=clock,
        )
