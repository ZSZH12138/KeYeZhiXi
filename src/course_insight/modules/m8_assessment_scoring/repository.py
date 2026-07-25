"""M8 repository boundary for papers and score-audit histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)


_AUDIT_TABLE = "m8_score_audits"


class M8Repository(Protocol):
    """Persistence operations owned exclusively by M8."""

    def save_paper(self, paper: AssessmentPaper) -> None:
        """Persist one immutable generated assessment paper."""

    def insert_or_get_paper(
        self,
        paper: AssessmentPaper,
        *,
        course_id: str,
        class_id: str,
    ) -> AssessmentPaper:
        """Persist paper and execution scope or return its identical winner."""

    def get_paper(self, paper_id: str) -> AssessmentPaper | None:
        """Load one frozen paper by identity."""

    def get_paper_execution_context(
        self,
        paper_id: str,
    ) -> tuple[str, str] | None:
        """Load the internal course/class event scope for one paper."""

    def save_score_audit(self, record: ScoreAuditRecord) -> None:
        """Append one exact score-audit version."""

    def get_score_audit(
        self,
        audit_id: str,
        audit_version: int,
    ) -> ScoreAuditRecord | None:
        """Load one exact score-audit version."""

    def insert_or_get_scoring_result(
        self,
        bundle: ScoringResultBundle,
    ) -> ScoringResultBundle:
        """Persist one complete scoring-result version idempotently."""

    def get_scoring_result(
        self,
        attempt_id: str,
    ) -> ScoringResultBundle | None:
        """Load the latest complete scoring result for an attempt."""

    def get_scoring_result_by_checksum(
        self,
        attempt_id: str,
        checksum: str,
    ) -> ScoringResultBundle | None:
        """Load one exact historical scoring result."""

    def get_scoring_result_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> ScoringResultBundle | None:
        """Load the earliest result at one exact target-audit version."""
