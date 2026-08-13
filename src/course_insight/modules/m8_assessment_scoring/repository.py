"""M8 repository boundary for papers and score-audit histories."""

from __future__ import annotations

from typing import Protocol

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionResult,
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTParameterSet,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)


_AUDIT_TABLE = "m8_score_audits"


class ReviewVersionConflictError(RuntimeError):
    """The persisted audit winner no longer matches a review request."""


def assert_review_transition(
    current: ScoringResultBundle,
    reviewed: ScoringResultBundle,
    *,
    audit_id: str,
    expected_audit_version: int,
    expected_audit_checksum: str,
) -> None:
    """Validate one append-only review against the transaction-time winner."""

    try:
        current_audit = current.get_audit_record(audit_id)
        reviewed_audit = reviewed.get_audit_record(audit_id)
    except Exception as error:
        raise ReviewVersionConflictError(
            "review audit is unavailable from the persisted scoring vector"
        ) from error
    if (
        current_audit.audit_version != expected_audit_version
        or current_audit.content_checksum() != expected_audit_checksum
        or reviewed_audit.audit_version != expected_audit_version + 1
    ):
        raise ReviewVersionConflictError(
            "review base audit version or checksum changed"
        )
    current_history = {
        (record.audit_id, record.audit_version): record
        for record in current.score_audit_records
    }
    reviewed_history = {
        (record.audit_id, record.audit_version): record
        for record in reviewed.score_audit_records
    }
    expected_review_key = (audit_id, expected_audit_version + 1)
    if reviewed_history.keys() != current_history.keys() | {
        expected_review_key
    } or any(
        record.content_checksum()
        != reviewed_history[history_key].content_checksum()
        for history_key, record in current_history.items()
    ):
        raise ReviewVersionConflictError(
            "the scoring history changed before review"
        )


class M8Repository(Protocol):
    """Persistence operations owned exclusively by M8."""

    def save_paper(self, paper: AssessmentPaper) -> None:
        """Persist one immutable generated assessment paper."""

    def insert_or_get_paper_record(
        self,
        record: FrozenAssessmentRecord,
    ) -> FrozenAssessmentRecord:
        """Persist or recover one identical paper, scope, and rubric record."""

    def get_paper_record(
        self,
        paper_id: str,
    ) -> FrozenAssessmentRecord | None:
        """Load the authoritative frozen record for a generated paper."""

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

    def insert_or_get_reviewed_scoring_result(
        self,
        bundle: ScoringResultBundle,
        *,
        audit_id: str,
        expected_audit_version: int,
        expected_audit_checksum: str,
    ) -> ScoringResultBundle:
        """Atomically compare and append one teacher-reviewed audit version."""

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

    def insert_or_get_calibration_run(
        self,
        result: CalibrationRunResult,
        *,
        course_id: str,
    ) -> CalibrationRunResult:
        """Persist one run and its immutable parameter snapshot atomically."""

    def get_calibration_run(
        self,
        run_id: str,
    ) -> CalibrationRunResult | None:
        """Load one exact calibration run."""

    def get_calibration_run_course_id(self, run_id: str) -> str | None:
        """Load the persisted course scope for one calibration run."""

    def insert_or_get_parameter_set(
        self,
        parameter_set: IRTParameterSet,
        *,
        course_id: str,
    ) -> IRTParameterSet:
        """Persist one immutable course-scoped IRT parameter version."""

    def get_parameter_set(
        self,
        parameter_set_id: str,
    ) -> IRTParameterSet | None:
        """Load one exact IRT parameter snapshot."""

    def get_parameter_set_course_id(self, parameter_set_id: str) -> str | None:
        """Load the persisted course scope for one parameter snapshot."""

    def list_parameter_sets(self, *, course_id: str) -> list[IRTParameterSet]:
        """List all immutable parameter versions for one course."""

    def insert_or_get_calibration_review(
        self,
        decision: CalibrationReviewDecision,
        reviewed_parameter_set: IRTParameterSet,
        *,
        course_id: str,
    ) -> tuple[CalibrationReviewDecision, IRTParameterSet]:
        """Persist one review and its resulting parameter version atomically."""

    def get_calibration_review(
        self,
        calibration_run_id: str,
    ) -> CalibrationReviewDecision | None:
        """Load the review recorded for one calibration run."""

    def insert_or_get_ability_estimate(
        self,
        estimate: AbilityEstimate,
        *,
        course_id: str,
    ) -> AbilityEstimate:
        """Persist one immutable course-scoped learner ability estimate."""

    def get_ability_estimate(
        self,
        estimate_id: str,
    ) -> AbilityEstimate | None:
        """Load one exact ability estimate."""

    def insert_or_get_adaptive_selection(
        self,
        selection: AdaptiveSelectionResult,
        *,
        course_id: str,
    ) -> AdaptiveSelectionResult:
        """Persist one immutable adaptive item-selection decision."""

    def get_adaptive_selection(
        self,
        selection_id: str,
    ) -> AdaptiveSelectionResult | None:
        """Load one exact adaptive item-selection decision."""


__all__ = [
    "M8Repository",
    "ReviewVersionConflictError",
    "assert_review_transition",
]
