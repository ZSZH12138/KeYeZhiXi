"""Authoritative reads and replay validation for split assessment use cases."""

from __future__ import annotations

from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.workflow import AssessmentRun
from course_insight.modules.m9_teacher_analytics.service import (
    teacher_analytics_report_id,
)


class AssessmentResults:
    """Recover module-owned results referenced by payload-free M0 metadata."""

    def __init__(self, **services: Any) -> None:
        self._m0 = services["m0"]
        self._m4 = services["m4"]
        self._m5 = services["m5"]
        self._m6 = services["m6"]
        self._m7 = services["m7"]
        self._m8 = services["m8"]
        self._m9 = services["m9"]

    def require_start(
        self,
        paper_id: str,
        *,
        learner_id: str | None = None,
    ) -> AssessmentRun:
        run = self._m0.get_assessment_run_by_paper(
            paper_id,
            operation="start",
        )
        if (
            run is None
            or run.status != "completed"
            or learner_id is not None
            and run.learner_id != learner_id
        ):
            raise DomainError(
                code="ASSESSMENT_NOT_FOUND",
                module="application",
                message="assessment is unavailable in the requested scope",
                recoverable=True,
            )
        return run

    def require_scope(
        self,
        paper_id: str,
        course_id: str,
        class_id: str,
    ) -> AssessmentRun:
        run = self.require_start(paper_id)
        if run.course_id != course_id or run.class_id != class_id:
            raise DomainError(
                code="ASSESSMENT_SCOPE_MISMATCH",
                module="application",
                message="assessment is outside the requested teaching scope",
                recoverable=True,
            )
        return run

    def completed_submit(self, paper_id: str) -> AssessmentRun:
        run = self._m0.get_assessment_run_by_paper(
            paper_id,
            operation="submit",
        )
        if run is None or run.status != "completed":
            missing("assessment submission is not complete")
        return run

    def effective_completed_run(
        self,
        paper_id: str,
        *,
        exclude_operation_id: str | None = None,
    ) -> AssessmentRun:
        review = self._m0.get_assessment_run_by_paper(
            paper_id,
            operation="review",
            status="completed",
        )
        if review is not None and review.operation_id != exclude_operation_id:
            return review
        return self.completed_submit(paper_id)

    def require_task(self, task_id: str):
        value = self._m4.get_task_plan(task_id)
        if value is None:
            missing("assessment task is unavailable")
        return value

    def require_paper(self, paper_id: str):
        value = self._m8.get_paper(paper_id)
        if value is None:
            missing("assessment paper is unavailable")
        return value

    def analytics_for(self, state):
        return self._m9.get_analytics(
            teacher_analytics_report_id(
                course_id=state.class_state_snapshot.course_id,
                class_state_snapshot_id=state.class_state_snapshot.snapshot_id,
            )
        )

    def exact_scoring(self, run: AssessmentRun):
        if run.scoring_result_checksum is None:
            return None
        return self._m8.get_scoring_result_by_checksum(
            run.attempt_id,
            run.scoring_result_checksum,
        )

    def exact_state(self, run: AssessmentRun):
        if run.state_version is None:
            return None
        return self._m5.get_state_update_version(
            run.attempt_id,
            run.state_version,
        )

    def submission_result(self, run: AssessmentRun):
        task = self.require_task(run.task_id)
        paper = self.require_paper(run.paper_id)
        scoring = self.exact_scoring(run)
        state = self.exact_state(run)
        feedback = self._m7.get_feedback(run.feedback_id)
        analytics = self._m9.get_analytics(run.report_id)
        require_results(scoring, state, feedback, analytics)
        tutoring = self._m6.decide_next_action(
            task_plan=task,
            scoring_result_bundle=scoring,
            state_update_result=state,
            previous_session_state_snapshot=None,
        )
        return {
            "task_plan": task,
            "assessment_paper": paper,
            "scoring_result": scoring,
            "state_result": state,
            "tutoring_result": tutoring,
            "feedback": feedback,
            "analytics": analytics,
        }

    def review_result(self, run: AssessmentRun, decision_id: str):
        decision = self._m9.get_review_decision(decision_id)
        scoring = self.exact_scoring(run)
        state = self.exact_state(run)
        analytics = self._m9.get_analytics(run.report_id)
        require_results(decision, scoring, state, analytics)
        return {
            "review_decision": decision,
            "reviewed_scoring_result": scoring,
            "recomputed_state_result": state,
            "refreshed_analytics": analytics,
        }


def validate_decision(decision, submission) -> None:
    values = (
        (decision.decision_id, submission.submission_id),
        (decision.audit_id, submission.audit_id),
        (
            decision.expected_audit_version,
            submission.expected_audit_version,
        ),
        (decision.reviewer_id, submission.reviewer_id),
        (decision.decision, submission.decision),
        (decision.final_total_score, submission.final_total_score),
    )
    if any(left != right for left, right in values):
        raise DomainError(
            code="REVIEW_SUBMISSION_CONFLICT",
            module="application",
            message="teacher review replay does not match its decision",
            recoverable=True,
        )


def existing_review(current, decision):
    record = current.get_audit_record(decision.audit_id)
    if record.audit_version == decision.expected_audit_version:
        return None
    if (
        record.audit_version == decision.expected_audit_version + 1
        and record.scoring_method == "teacher_override"
        and record.total_score == decision.final_total_score
        and record.review_status
        == (
            "rejected_pending_rescore"
            if decision.is_reject()
            else "approved"
        )
    ):
        return current
    raise DomainError(
        code="REVIEW_VERSION_CONFLICT",
        module="application",
        message="persisted review audit does not match the decision",
        recoverable=True,
    )


def require_results(*values: Any) -> None:
    if any(value is None for value in values):
        missing("authoritative workflow result is unavailable")


def missing(message: str):
    raise DomainError(
        code="WORKFLOW_RESULT_MISSING",
        module="application",
        message=message,
        recoverable=True,
    )
