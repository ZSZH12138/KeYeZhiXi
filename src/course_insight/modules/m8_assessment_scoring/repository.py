"""M8 repository boundary for papers and score-audit histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.assessment import AssessmentPaper, ScoreAuditRecord


_AUDIT_TABLE = "m8_score_audits"


class M8Repository(Protocol):
    """Persistence operations owned exclusively by M8."""

    def save_paper(self, paper: AssessmentPaper) -> None:
        """Persist one immutable generated assessment paper."""

    def save_score_audit(self, record: ScoreAuditRecord) -> None:
        """Append one exact score-audit version."""

    def get_score_audit(
        self,
        audit_id: str,
        audit_version: int,
    ) -> ScoreAuditRecord | None:
        """Load one exact score-audit version."""
