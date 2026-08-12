"""Restart-safe split assessment use cases for the application layer."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from course_insight.application.assessment_dependencies import (
    AssessmentDependencies,
    capture_assessment_dependencies,
    policy_execution_run_fields,
    verify_policy_execution,
    verify_policy_dependencies,
)
from course_insight.application.assessment_recovery import (
    AssessmentRecovery,
    dependencies_from_run,
    ensure_start_dependencies,
)
from course_insight.application.assessment_results import (
    AssessmentResults,
    existing_review,
    missing,
    require_results,
    validate_decision,
)
from course_insight.application.retrieval import retrieve_for_application
from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.platform import (
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.modules.m0_platform.workflow import AssessmentRun


class AssessmentWorkflow:
    """Coordinate module-owned results through payload-free M0 metadata."""

    _lease_seconds = 30.0
    _heartbeat_interval_seconds = 10.0

    def __init__(self, **services: Any) -> None:
        self._m0 = services["m0"]
        self._m2 = services["m2"]
        self._m4 = services["m4"]
        self._m5 = services["m5"]
        self._m6 = services["m6"]
        self._m7 = services["m7"]
        self._m8 = services["m8"]
        self._m9 = services["m9"]
        self._recovery = AssessmentRecovery(
            m0=self._m0,
            m5=self._m5,
            now=self._now,
        )
        self._results = AssessmentResults(**services)

    def start(
        self,
        *,
        student_text: str,
        task_type_hint: str | None,
        course_id: str,
        class_id: str,
        learner_id: str,
        session_id: str,
        knowledge_bundle: KnowledgeBundle,
    ) -> dict[str, ContractModel]:
        dependencies = capture_assessment_dependencies(
            knowledge_bundle=knowledge_bundle,
            evidence_index_ref=None,
            state_policy_path=None,
            teacher_policy_path=None,
        )
        learner_state = self._m5.get_latest_learner_state(
            course_id,
            class_id,
            learner_id,
        )
        task = self._m4.create_task_plan(
            student_text=student_text,
            task_type_hint=task_type_hint,
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            knowledge_bundle=knowledge_bundle,
            learner_state_snapshot=learner_state,
        )
        paper = self._m8.generate_paper(
            task_plan=task,
            knowledge_bundle=knowledge_bundle,
            learner_state_snapshot=learner_state,
            diagnosis_result=None,
        )
        now = self._now()
        operation_id = f"start:{task.task_id}"
        run = self._m0.record_assessment_run(
            AssessmentRun(
                operation_id=operation_id,
                operation="start",
                request_checksum=self._checksum(
                    task.content_checksum(),
                    paper.content_checksum(),
                ),
                course_id=course_id,
                class_id=class_id,
                learner_id=learner_id,
                session_id=session_id,
                task_id=task.task_id,
                paper_id=paper.paper_id,
                attempt_id=None,
                feedback_id=None,
                report_id=None,
                checkpoint="pending",
                status="pending",
                version=1,
                locked_by=None,
                lease_until=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                **dependencies.as_run_fields(),
            )
        )
        if run.status != "completed":
            run = self._claim(run, operation_id)
            run = self._advance(run, "task_saved")
            run = self._advance(run, "paper_saved")
            self._complete(run)
        return {"task_plan": task, "assessment_paper": paper}

    def pending_assessment(
        self,
        *,
        paper_id: str,
        learner_id: str,
    ) -> dict[str, ContractModel]:
        """Recover the authoritative paper for a student-facing PRG page."""

        start = self._results.require_start(paper_id, learner_id=learner_id)
        submitted = self._m0.get_assessment_run_by_paper(
            paper_id,
            operation="submit",
            status="completed",
        )
        if submitted is not None:
            raise DomainError(
                code="ASSESSMENT_ALREADY_SUBMITTED",
                module="application",
                message="assessment submission is already complete",
                recoverable=True,
            )
        task = self._results.require_task(start.task_id)
        paper = self._results.require_paper(start.paper_id)
        if (
            self._checksum(
                task.content_checksum(),
                paper.content_checksum(),
            )
            != start.request_checksum
        ):
            missing("assessment start references are inconsistent")
        return {"task_plan": task, "assessment_paper": paper}

    def submit(
        self,
        *,
        assessment_submission: AssessmentSubmission,
        request_id: str,
        index_ref: EvidenceIndexRef,
        knowledge_bundle: KnowledgeBundle,
        state_policy_path: Path,
        teacher_threshold_policy_path: Path,
    ) -> dict[str, ContractModel]:
        start = self._results.require_start(
            assessment_submission.paper_id,
            learner_id=assessment_submission.learner_id,
        )
        dependencies = capture_assessment_dependencies(
            knowledge_bundle=knowledge_bundle,
            evidence_index_ref=index_ref,
            state_policy_path=state_policy_path,
            teacher_policy_path=teacher_threshold_policy_path,
        )
        if start.knowledge_bundle_id is None:
            frozen_task = self._results.require_task(start.task_id)
            start = self._recovery.adopt_legacy_dependencies(
                start,
                dependencies,
                frozen_knowledge_bundle_id=(
                    frozen_task.knowledge_bundle_id
                ),
                frozen_course_package_id=frozen_task.course_package_id,
            )
        ensure_start_dependencies(start, dependencies)
        run = self._record(
            operation_id=f"submit:{assessment_submission.submission_id}",
            operation="submit",
            checksum=assessment_submission.content_checksum(),
            start=start,
            attempt_id=assessment_submission.attempt_id,
            dependencies=dependencies,
        )
        if run.status == "completed":
            return self._results.submission_result(run)
        run = self._claim(run, request_id)
        try:
            frozen_dependencies = dependencies_from_run(run)
            expected_state_policy_checksum = self._required_policy_checksum(
                frozen_dependencies.state_policy_checksum
            )
            expected_teacher_policy_checksum = self._required_policy_checksum(
                frozen_dependencies.teacher_policy_checksum
            )
            task = self._results.require_task(run.task_id)
            paper = self._results.require_paper(run.paper_id)
            scoring = self._results.exact_scoring(run)
            if scoring is None and run.scoring_result_checksum is None:
                scoring = self._m8.get_scoring_result(run.attempt_id)
            if scoring is None:
                preparation = self._execute(
                    run,
                    lambda: self._m8.prepare_scoring(
                        assessment_paper=paper,
                        raw_answer_path=assessment_submission,
                        knowledge_bundle=knowledge_bundle,
                    ),
                )
                rubric_results = []
                for scoring_task in preparation.rubric_scoring_tasks:
                    query = preparation.query_for_task(
                        scoring_task.scoring_task_id
                    )
                    evidence = self._execute(
                        run,
                        lambda query=query: retrieve_for_application(
                            self._m2,
                            query,
                            index_ref,
                            request_id=f"{run.operation_id}:grading:{query.query_id}",
                        ),
                    )
                    rubric_results.append(
                        self._execute(
                            run,
                            lambda scoring_task=scoring_task, evidence=evidence: (
                                self._m7.score_subjective_answer(
                                    rubric_scoring_task=scoring_task,
                                    evidence_bundle=evidence,
                                )
                            ),
                        )
                    )
                scoring = self._execute(
                    run,
                    lambda: self._m8.finalize_scoring(
                        scoring_preparation_result=preparation,
                        rubric_scoring_results=rubric_results,
                    ),
                )
            if run.checkpoint == "claimed":
                run = self._advance(
                    run,
                    "scoring_saved",
                    scoring_result_checksum=scoring.content_checksum(),
                )
            if run.checkpoint == "scoring_saved":
                self._m0.append_learning_events(scoring.learning_events)
                run = self._advance(run, "events_appended")

            state = self._results.exact_state(run)
            if state is None and run.state_version is None:
                state = self._m5.get_state_update(run.attempt_id)
            if run.checkpoint == "events_appended":
                previous_learner = None
                previous_class = None
                if state is None:
                    previous_learner = self._m5.get_latest_learner_state(
                        run.course_id,
                        run.class_id,
                        run.learner_id,
                    )
                    previous_class = self._m5.get_latest_class_state(
                        run.course_id,
                        run.class_id,
                    )
                run = self._recovery.freeze_state_inputs(
                    run,
                    learner=previous_learner,
                    class_state=previous_class,
                )
            if state is None:
                if run.state_version is not None:
                    missing("assessment state result is unavailable")
                previous_learner, previous_class = (
                    self._recovery.load_state_inputs(run)
                )
                verify_policy_dependencies(
                    frozen_dependencies,
                    state_policy_path=state_policy_path,
                    teacher_policy_path=teacher_threshold_policy_path,
                )
                state = self._execute(
                    run,
                    lambda: self._m5.update_state_with_frozen_policy(
                        scoring_result_bundle=scoring,
                        knowledge_bundle=knowledge_bundle,
                        previous_learner_state_snapshot=previous_learner,
                        previous_class_state_snapshot=previous_class,
                        state_policy_path=state_policy_path,
                        expected_policy_checksum=(
                            expected_state_policy_checksum
                        ),
                    ),
                )
            if run.checkpoint == "state_inputs_frozen":
                run = self._advance(
                    run,
                    "state_saved",
                    state_version=state.learner_state_snapshot.state_version,
                )

            policy_execution = self._execute(
                run,
                lambda: self._m6.prepare_policy_execution(
                    task_plan=task,
                    scoring_result_bundle=scoring,
                    state_update_result=state,
                    previous_session_state_snapshot=None,
                ),
            )
            if run.checkpoint == "state_saved":
                run = self._advance(
                    run,
                    "policy_frozen",
                    **policy_execution_run_fields(policy_execution),
                )
            elif run.policy_id is None:
                run = self._m0.adopt_legacy_assessment_run(
                    replace(
                        run,
                        updated_at=self._now(),
                        **policy_execution_run_fields(policy_execution),
                    )
                )
            else:
                verify_policy_execution(run, policy_execution)

            tutoring = self._execute(
                run,
                lambda: self._m6.decide_next_action(
                    task_plan=task,
                    scoring_result_bundle=scoring,
                    state_update_result=state,
                    previous_session_state_snapshot=None,
                ),
            )
            if run.checkpoint == "policy_frozen":
                run = self._advance(run, "tutoring_saved")

            feedback = self._m7.get_feedback_for_task(
                run.task_id,
                run.learner_id,
            )
            if feedback is None:
                evidence = self._execute(
                    run,
                    lambda: retrieve_for_application(
                        self._m2,
                        tutoring.evidence_query,
                        index_ref,
                        request_id=f"{run.operation_id}:feedback:{tutoring.evidence_query.query_id}",
                    ),
                )
                feedback = self._execute(
                    run,
                    lambda: self._m7.generate_student_feedback(
                        feedback_generation_task=(
                            tutoring.feedback_generation_task
                        ),
                        evidence_bundle=evidence,
                    ),
                )
            if run.checkpoint == "tutoring_saved":
                run = self._advance(
                    run,
                    "feedback_saved",
                    feedback_id=feedback.feedback_id,
                )

            analytics = self._results.analytics_for(state)
            if analytics is None:
                verify_policy_dependencies(
                    frozen_dependencies,
                    state_policy_path=state_policy_path,
                    teacher_policy_path=teacher_threshold_policy_path,
                )
                analytics = self._execute(
                    run,
                    lambda: self._m9.build_teacher_analytics_with_frozen_policy(
                        knowledge_bundle=knowledge_bundle,
                        scoring_result_bundle=scoring,
                        state_update_result=state,
                        teacher_threshold_policy_path=(
                            teacher_threshold_policy_path
                        ),
                        expected_policy_checksum=(
                            expected_teacher_policy_checksum
                        ),
                    ),
                )
            if run.checkpoint == "feedback_saved":
                run = self._advance(
                    run,
                    "analytics_saved",
                    report_id=analytics.report_id,
                )
            if run.checkpoint == "analytics_saved":
                self._complete(run)
            return {
                "task_plan": task,
                "assessment_paper": paper,
                "scoring_result": scoring,
                "state_result": state,
                "tutoring_result": tutoring,
                "feedback": feedback,
                "analytics": analytics,
            }
        except DomainError as error:
            self._fail(run, error.code)
            raise
        except Exception as error:
            self._fail(run, "WORKFLOW_EXECUTION_FAILED")
            raise self._execution_error("assessment") from error

    def student_result(
        self,
        *,
        paper_id: str,
        learner_id: str,
    ) -> dict[str, ContractModel]:
        self._results.require_start(paper_id, learner_id=learner_id)
        run = self._results.effective_completed_run(paper_id)
        paper = self._results.require_paper(paper_id)
        scoring = self._results.exact_scoring(run)
        feedback = self._m7.get_feedback(run.feedback_id)
        require_results(scoring, feedback)
        return {
            "assessment_paper": paper,
            "scoring_result": scoring,
            "feedback": feedback,
        }

    def teacher_context(
        self,
        *,
        paper_id: str,
        course_id: str,
        class_id: str,
    ) -> dict[str, ContractModel]:
        start = self._results.require_scope(paper_id, course_id, class_id)
        run = self._results.effective_completed_run(paper_id)
        paper = self._results.require_paper(start.paper_id)
        scoring = self._results.exact_scoring(run)
        state = self._results.exact_state(run)
        analytics = self._m9.get_analytics(run.report_id)
        require_results(paper, scoring, state, analytics)
        return {
            "assessment_paper": paper,
            "scoring_result": scoring,
            "state_result": state,
            "analytics": analytics,
        }

    def review(
        self,
        *,
        paper_id: str,
        review_submission: TeacherReviewSubmission,
        request_id: str,
        knowledge_bundle: KnowledgeBundle,
        state_policy_path: Path,
        teacher_threshold_policy_path: Path,
        course_id: str,
        class_id: str,
    ) -> dict[str, ContractModel]:
        start = self._results.require_scope(paper_id, course_id, class_id)
        submit = self._results.completed_submit(paper_id)
        dependencies = capture_assessment_dependencies(
            knowledge_bundle=knowledge_bundle,
            evidence_index_ref=None,
            state_policy_path=state_policy_path,
            teacher_policy_path=teacher_threshold_policy_path,
        )
        if start.knowledge_bundle_id is None:
            frozen_task = self._results.require_task(start.task_id)
            start = self._recovery.adopt_legacy_dependencies(
                start,
                dependencies,
                frozen_knowledge_bundle_id=(
                    frozen_task.knowledge_bundle_id
                ),
                frozen_course_package_id=frozen_task.course_package_id,
            )
        ensure_start_dependencies(start, dependencies)
        operation_id = f"review:{review_submission.submission_id}"
        run = self._record(
            operation_id=operation_id,
            operation="review",
            checksum=review_submission.content_checksum(),
            start=start,
            attempt_id=submit.attempt_id,
            feedback_id=submit.feedback_id,
            report_id=submit.report_id,
            target_audit_id=review_submission.audit_id,
            target_audit_version=review_submission.expected_audit_version,
            dependencies=dependencies,
        )
        if run.status == "completed":
            return self._results.review_result(
                run,
                review_submission.submission_id,
            )
        run = self._claim(run, request_id)
        try:
            frozen_dependencies = dependencies_from_run(run)
            expected_state_policy_checksum = self._required_policy_checksum(
                frozen_dependencies.state_policy_checksum
            )
            expected_teacher_policy_checksum = self._required_policy_checksum(
                frozen_dependencies.teacher_policy_checksum
            )
            base_run = self._results.effective_completed_run(
                paper_id,
                exclude_operation_id=run.operation_id,
            )
            current = self._results.exact_scoring(base_run)
            require_results(current)
            decision = self._m9.get_review_decision(
                review_submission.submission_id
            )
            if decision is None:
                decision = self._execute(
                    run,
                    lambda: self._m9.record_teacher_review(
                        raw_review_path=review_submission,
                        current_scoring_result_bundle=current,
                    ),
                )
            validate_decision(decision, review_submission)
            if run.checkpoint == "claimed":
                run = self._advance(run, "decision_saved")

            reviewed = self._results.exact_scoring(run)
            if reviewed is None:
                reviewed = self._m8.get_scoring_result_for_audit(
                    run.attempt_id,
                    decision.audit_id,
                    decision.expected_audit_version + 1,
                )
            existing = existing_review(reviewed or current, decision)
            reviewed = reviewed or existing
            if reviewed is None:
                reviewed = self._execute(
                    run,
                    lambda: self._m8.apply_teacher_review(
                        current_scoring_result_bundle=current,
                        teacher_review_decision=decision,
                    ),
                )
            if run.checkpoint == "decision_saved":
                run = self._advance(
                    run,
                    "review_saved",
                    scoring_result_checksum=reviewed.content_checksum(),
                )
            if run.checkpoint == "review_saved":
                self._m0.append_learning_events(reviewed.learning_events)
                run = self._advance(run, "events_appended")

            state = self._results.exact_state(run)
            if state is None:
                state = self._m5.get_state_update_for_audit(
                    run.attempt_id,
                    decision.audit_id,
                    decision.expected_audit_version + 1,
                )
            review_key = (
                f"{decision.audit_id}:{decision.expected_audit_version + 1}"
            )
            if run.checkpoint == "events_appended":
                previous_state = self._results.exact_state(base_run)
                require_results(previous_state)
                run = self._recovery.freeze_state_inputs(
                    run,
                    learner=previous_state.learner_state_snapshot,
                    class_state=previous_state.class_state_snapshot,
                )
            if state is None or review_key not in state.processed_audit_ids:
                if run.checkpoint != "state_inputs_frozen":
                    missing("review state result is unavailable")
                previous_learner, previous_class = (
                    self._recovery.load_state_inputs(run)
                )
                verify_policy_dependencies(
                    frozen_dependencies,
                    state_policy_path=state_policy_path,
                    teacher_policy_path=teacher_threshold_policy_path,
                )
                state = self._execute(
                    run,
                    lambda: self._m5.update_state_with_frozen_policy(
                        scoring_result_bundle=reviewed,
                        knowledge_bundle=knowledge_bundle,
                        previous_learner_state_snapshot=previous_learner,
                        previous_class_state_snapshot=previous_class,
                        state_policy_path=state_policy_path,
                        expected_policy_checksum=(
                            expected_state_policy_checksum
                        ),
                    ),
                )
            if run.checkpoint == "state_inputs_frozen":
                run = self._advance(
                    run,
                    "state_saved",
                    state_version=state.learner_state_snapshot.state_version,
                )

            analytics = self._results.analytics_for(state)
            if analytics is None:
                verify_policy_dependencies(
                    frozen_dependencies,
                    state_policy_path=state_policy_path,
                    teacher_policy_path=teacher_threshold_policy_path,
                )
                analytics = self._execute(
                    run,
                    lambda: self._m9.build_teacher_analytics_with_frozen_policy(
                        knowledge_bundle=knowledge_bundle,
                        scoring_result_bundle=reviewed,
                        state_update_result=state,
                        teacher_threshold_policy_path=(
                            teacher_threshold_policy_path
                        ),
                        expected_policy_checksum=(
                            expected_teacher_policy_checksum
                        ),
                    ),
                )
            if run.checkpoint == "state_saved":
                run = self._advance(
                    run,
                    "analytics_saved",
                    report_id=analytics.report_id,
                )
            if run.checkpoint == "analytics_saved":
                self._complete(run)
            return {
                "review_decision": decision,
                "reviewed_scoring_result": reviewed,
                "recomputed_state_result": state,
                "refreshed_analytics": analytics,
            }
        except DomainError as error:
            self._fail(run, error.code)
            raise
        except Exception as error:
            self._fail(run, "WORKFLOW_EXECUTION_FAILED")
            raise self._execution_error("teacher review") from error

    def _record(
        self,
        *,
        operation_id: str,
        operation: str,
        checksum: str,
        start: AssessmentRun,
        attempt_id: str,
        dependencies: AssessmentDependencies,
        feedback_id: str | None = None,
        report_id: str | None = None,
        target_audit_id: str | None = None,
        target_audit_version: int | None = None,
    ) -> AssessmentRun:
        if not operation_id:
            raise DomainError(
                code="WORKFLOW_REQUEST_INVALID",
                module="application",
                message="workflow request identity is required",
            )
        now = self._now()
        return self._m0.record_assessment_run(
            AssessmentRun(
                operation_id=operation_id,
                operation=operation,
                request_checksum=checksum,
                course_id=start.course_id,
                class_id=start.class_id,
                learner_id=start.learner_id,
                session_id=start.session_id,
                task_id=start.task_id,
                paper_id=start.paper_id,
                attempt_id=attempt_id,
                feedback_id=feedback_id,
                report_id=report_id,
                checkpoint="pending",
                status="pending",
                version=1,
                locked_by=None,
                lease_until=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                target_audit_id=target_audit_id,
                target_audit_version=target_audit_version,
                previous_state_frozen=False,
                **dependencies.as_run_fields(),
            )
        )

    def _claim(self, run: AssessmentRun, worker_id: str) -> AssessmentRun:
        return self._recovery.claim(
            run,
            worker_id,
            lease_seconds=self._lease_seconds,
        )

    def _advance(
        self,
        run: AssessmentRun,
        checkpoint: str,
        **refs: object,
    ) -> AssessmentRun:
        return self._recovery.advance(run, checkpoint, **refs)

    def _complete(self, run: AssessmentRun) -> AssessmentRun:
        return self._recovery.complete(run)

    def _fail(self, run: AssessmentRun, code: str) -> None:
        self._recovery.fail(run, code)

    def _execute(
        self,
        run: AssessmentRun,
        action: Callable[[], Any],
    ) -> Any:
        return self._recovery.execute(
            run,
            action,
            lease_seconds=self._lease_seconds,
            heartbeat_interval_seconds=self._heartbeat_interval_seconds,
        )

    @staticmethod
    def _execution_error(operation: str) -> DomainError:
        return DomainError(
            code="WORKFLOW_EXECUTION_FAILED",
            module="application",
            message=f"{operation} workflow could not be completed",
            recoverable=True,
        )

    @staticmethod
    def _checksum(*values: str) -> str:
        return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()

    @staticmethod
    def _required_policy_checksum(value: str | None) -> str:
        if value is None:
            raise DomainError(
                code="WORKFLOW_DEPENDENCY_UNAVAILABLE",
                module="application",
                message="assessment policy identity is unavailable",
                recoverable=True,
            )
        return value

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
