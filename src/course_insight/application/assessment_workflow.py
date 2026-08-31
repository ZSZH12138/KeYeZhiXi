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
from course_insight.application.assessment_evidence_binding import (
    bind_release_rubric_evidence,
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
from course_insight.contracts.assessment import (
    RubricScoringTask,
    ScoringPreparationResult,
    ScoringResultBundle,
)
from course_insight.application.class_roster import ClassRosterSnapshot
from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceBundle, EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import LearningObservationBatch
from course_insight.contracts.platform import (
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.contracts.tasking import PROFILE_AFFECTING_TASK_TYPES
from course_insight.contracts.tutoring import StudentFeedbackPackage
from course_insight.contracts.intelligence import RetrievalPolicy
from course_insight.modules.m0_platform.workflow import (
    WAITING_WORKFLOW_STATUSES,
    AssessmentRun,
)
from course_insight.modules.m8_assessment_scoring.selection_policy import (
    AssessmentSelectionContext,
)


def _same_review_outcome(decision: Any, requested: TeacherReviewSubmission) -> bool:
    """Compare the immutable scoring outcome while allowing a retried UI note."""

    decision_overrides = sorted(
        (
            override.criterion_id,
            override.previous_score,
            override.new_score,
        )
        for override in decision.criterion_overrides
    )
    requested_overrides = sorted(
        (
            override.criterion_id,
            override.previous_score,
            override.new_score,
        )
        for override in requested.criterion_overrides
    )
    return (
        decision.audit_id == requested.audit_id
        and decision.expected_audit_version == requested.expected_audit_version
        and decision.expected_audit_checksum == requested.expected_audit_checksum
        and decision.reviewer_id == requested.reviewer_id
        and decision.decision == requested.decision
        and decision.final_total_score == requested.final_total_score
        and decision_overrides == requested_overrides
    )


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
        self._retrieval_policy: RetrievalPolicy | None = services.get(
            "retrieval_policy"
        )
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
        selection_context: AssessmentSelectionContext | None = None,
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
        generate_arguments = {
            "task_plan": task,
            "knowledge_bundle": knowledge_bundle,
            "learner_state_snapshot": learner_state,
            "diagnosis_result": None,
        }
        if selection_context is not None:
            generate_arguments["selection_context"] = selection_context
        paper = self._m8.generate_paper(**generate_arguments)
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
        )
        if submitted is not None and submitted.status in {
            "completed",
            *WAITING_WORKFLOW_STATUSES,
        }:
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
        class_roster_snapshot: ClassRosterSnapshot | None = None,
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
            class_roster_snapshot=class_roster_snapshot,
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
        self._persist_frozen_submission(assessment_submission)
        if run.status == "completed":
            return self._results.submission_result(run)
        if run.status in WAITING_WORKFLOW_STATUSES:
            paper = self._results.require_paper(run.paper_id)
            scoring = self._results.exact_scoring(run)
            task = self._results.require_task(run.task_id)
            return {
                "task_plan": task,
                "assessment_paper": paper,
                "scoring_result": scoring,
                "waiting_status": run.status,
            }
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
                bound_rubric_tasks = []
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
                            policy=self._retrieval_policy,
                        ),
                    )
                    bound_task = (
                        bind_release_rubric_evidence(scoring_task, evidence)
                        if isinstance(scoring_task, RubricScoringTask)
                        and isinstance(evidence, EvidenceBundle)
                        else scoring_task
                    )
                    bound_rubric_tasks.append(bound_task)
                    rubric_results.append(
                        self._score_constructed_response(
                            run,
                            bound_task,
                            evidence,
                        )
                    )
                if (
                    isinstance(preparation, ScoringPreparationResult)
                    and bound_rubric_tasks
                    != preparation.rubric_scoring_tasks
                ):
                    preparation = preparation.model_copy(
                        update={
                            "rubric_scoring_tasks": bound_rubric_tasks,
                        },
                        deep=True,
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
            if task.task_type not in PROFILE_AFFECTING_TASK_TYPES:
                feedback = self._m7.get_feedback_for_task(
                    run.task_id,
                    run.learner_id,
                )
                if feedback is None:
                    feedback = self._m7.save_transient_feedback(
                        self._transient_feedback(task, scoring)
                    )
                if run.checkpoint == "scoring_saved":
                    self._recovery.finish(
                        run,
                        feedback_id=feedback.feedback_id,
                    )
                return {
                    "task_plan": task,
                    "assessment_paper": paper,
                    "scoring_result": scoring,
                    "feedback": feedback,
                }
            if run.checkpoint == "scoring_saved":
                if (
                    scoring.requires_teacher_review()
                    or scoring.has_rejected_score()
                ):
                    previous_learner = self._m5.get_latest_learner_state(
                        run.course_id,
                        run.class_id,
                        run.learner_id,
                    )
                    previous_class = self._m5.get_latest_class_state(
                        run.course_id,
                        run.class_id,
                    )
                    waiting_status = (
                        "awaiting_rescore"
                        if scoring.has_rejected_score()
                        else "awaiting_review"
                    )
                    self._recovery.park(
                        run,
                        waiting_status,
                        previous_state_frozen=True,
                        previous_learner_snapshot_id=(
                            None
                            if previous_learner is None
                            else previous_learner.snapshot_id
                        ),
                        previous_learner_state_version=(
                            None
                            if previous_learner is None
                            else previous_learner.state_version
                        ),
                        previous_class_snapshot_id=(
                            None
                            if previous_class is None
                            else previous_class.snapshot_id
                        ),
                        previous_class_state_version=None,
                    )
                    return {
                        "task_plan": task,
                        "assessment_paper": paper,
                        "scoring_result": scoring,
                        "waiting_status": waiting_status,
                    }
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
                        authoritative_class_id=run.class_id,
                        class_roster_size=(
                            frozen_dependencies.class_roster_size
                        ),
                        learning_observation_batch=(
                            self._build_observation_batch(scoring)
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
                        policy=self._retrieval_policy,
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
        task = self._results.require_task(run.task_id)
        if run.status in WAITING_WORKFLOW_STATUSES:
            scoring = self._results.exact_scoring(run)
            require_results(scoring)
            return {
                "task_plan": task,
                "assessment_paper": paper,
                "scoring_result": scoring,
                "waiting_status": run.status,
            }
        scoring = self._results.exact_scoring(run)
        feedback = self._m7.get_feedback(run.feedback_id)
        require_results(scoring, feedback)
        return {
            "task_plan": task,
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
        task = self._results.require_task(start.task_id)
        scoring = self._results.exact_scoring(run)
        if run.status in WAITING_WORKFLOW_STATUSES:
            require_results(paper, scoring)
            return {
                "task_plan": task,
                "assessment_paper": paper,
                "scoring_result": scoring,
                "waiting_status": run.status,
            }
        state = self._results.exact_state(run)
        analytics = self._m9.get_analytics(run.report_id)
        require_results(paper, scoring, state, analytics)
        return {
            "task_plan": task,
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
        index_ref: EvidenceIndexRef | None = None,
        class_roster_snapshot: ClassRosterSnapshot | None = None,
        student_evidence: str | None = None,
    ) -> dict[str, ContractModel]:
        start = self._results.require_scope(paper_id, course_id, class_id)
        submit = self._results.scored_submit(paper_id)
        dependencies = capture_assessment_dependencies(
            knowledge_bundle=knowledge_bundle,
            evidence_index_ref=None,
            state_policy_path=state_policy_path,
            teacher_policy_path=teacher_threshold_policy_path,
            class_roster_snapshot=class_roster_snapshot,
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
        review_submission = self._recoverable_failed_review_submission(
            paper_id,
            review_submission,
        )
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
            if run.state_version is None:
                scoring = self._results.exact_scoring(run)
                decision = self._m9.get_review_decision(
                    review_submission.submission_id
                )
                require_results(scoring, decision)
                return {
                    "review_decision": decision,
                    "scoring_result": scoring,
                    "waiting_status": (
                        "awaiting_rescore"
                        if scoring.has_rejected_score()
                        else "awaiting_review"
                    ),
                }
            return self._results.review_result(
                run,
                review_submission.submission_id,
            )
        retrying_failed = run.status == "failed"
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
                        student_evidence=student_evidence,
                    ),
                )
            if run.checkpoint == "decision_saved":
                run = self._advance(
                    run,
                    "review_saved",
                    scoring_result_checksum=reviewed.content_checksum(),
                )
            if run.checkpoint == "review_saved":
                if submit.status in WAITING_WORKFLOW_STATUSES and (
                    reviewed.requires_teacher_review()
                    or reviewed.has_rejected_score()
                ):
                    waiting_status = (
                        "awaiting_rescore"
                        if reviewed.has_rejected_score()
                        else "awaiting_review"
                    )
                    resumed = self._recovery.resume_parked(
                        submit,
                        worker_id=f"review:{submit.operation_id}",
                        lease_seconds=self._lease_seconds,
                    )
                    self._recovery.park(
                        resumed,
                        waiting_status,
                        scoring_result_checksum=reviewed.content_checksum(),
                    )
                    self._complete_waiting_review(run)
                    return {
                        "review_decision": decision,
                        "scoring_result": reviewed,
                        "waiting_status": waiting_status,
                    }
                self._m0.append_learning_events(reviewed.learning_events)
                run = self._advance(run, "events_appended")

            review_rejected = reviewed.has_rejected_score()
            state = self._results.exact_state(run)
            if state is None and not review_rejected:
                state = self._m5.get_state_update_for_audit(
                    run.attempt_id,
                    decision.audit_id,
                    decision.expected_audit_version + 1,
                )
            review_key = (
                f"{decision.audit_id}:{decision.expected_audit_version + 1}"
            )
            if run.checkpoint == "events_appended":
                previous_learner, previous_class = self._latest_state_inputs(run)
                run = self._recovery.freeze_state_inputs(
                    run,
                    learner=previous_learner,
                    class_state=previous_class,
                )
            if review_rejected and state is None:
                if submit.status in WAITING_WORKFLOW_STATUSES:
                    missing("rejected score is waiting for rescore")
                state = self._results.exact_state(base_run)
                require_results(state)
            if (
                not review_rejected
                and (
                    state is None
                    or review_key not in state.processed_audit_ids
                )
            ):
                if run.checkpoint != "state_inputs_frozen":
                    missing("review state result is unavailable")
                if retrying_failed:
                    latest_learner, latest_class = self._latest_state_inputs(run)
                    run = self._recovery.rebase_review_state_inputs(
                        run,
                        learner=latest_learner,
                        class_state=latest_class,
                    )
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
                        authoritative_class_id=run.class_id,
                        class_roster_size=(
                            frozen_dependencies.class_roster_size
                        ),
                        learning_observation_batch=(
                            self._build_observation_batch(reviewed)
                        ),
                    ),
                )
            if run.checkpoint == "state_inputs_frozen":
                run = self._advance(
                    run,
                    "state_saved",
                    state_version=state.learner_state_snapshot.state_version,
                )

            feedback = None
            if (
                submit.status in WAITING_WORKFLOW_STATUSES
                and not review_rejected
            ):
                feedback = self._feedback_for_waiting_commit(
                    run,
                    scoring=reviewed,
                    state=state,
                    index_ref=index_ref,
                )

            analytics = (
                None
                if review_rejected
                else self._results.analytics_for(state)
            )
            if analytics is None:
                verify_policy_dependencies(
                    frozen_dependencies,
                    state_policy_path=state_policy_path,
                    teacher_policy_path=teacher_threshold_policy_path,
                )

                def _build_review_analytics():
                    rejected_builder = getattr(
                        self._m9,
                        "build_rejected_score_analytics_with_frozen_policy",
                        None,
                    )
                    if review_rejected and callable(rejected_builder):
                        return rejected_builder(
                            knowledge_bundle=knowledge_bundle,
                            scoring_result_bundle=reviewed,
                            state_update_result=state,
                            teacher_threshold_policy_path=(
                                teacher_threshold_policy_path
                            ),
                            expected_policy_checksum=(
                                expected_teacher_policy_checksum
                            ),
                        )
                    return self._m9.build_teacher_analytics_with_frozen_policy(
                        knowledge_bundle=knowledge_bundle,
                        scoring_result_bundle=reviewed,
                        state_update_result=state,
                        teacher_threshold_policy_path=(
                            teacher_threshold_policy_path
                        ),
                        expected_policy_checksum=(
                            expected_teacher_policy_checksum
                        ),
                    )

                analytics = self._execute(run, _build_review_analytics)
            if run.checkpoint == "state_saved":
                run = self._advance(
                    run,
                    "analytics_saved",
                    report_id=analytics.report_id,
                    feedback_id=(
                        None if feedback is None else feedback.feedback_id
                    ),
                )
            if run.checkpoint == "analytics_saved":
                self._complete(run)
                if submit.status in WAITING_WORKFLOW_STATUSES:
                    self._finish_waiting_submit(
                        submit,
                        scoring_result_checksum=reviewed.content_checksum(),
                        state_version=state.learner_state_snapshot.state_version,
                        report_id=analytics.report_id,
                        feedback_id=(
                            None if feedback is None else feedback.feedback_id
                        ),
                    )
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

    def _latest_state_inputs(self, run: AssessmentRun):
        """Read the current profile baselines for a delayed review commit."""

        return (
            self._m5.get_latest_learner_state(
                run.course_id,
                run.class_id,
                run.learner_id,
            ),
            self._m5.get_latest_class_state(
                run.course_id,
                run.class_id,
            ),
        )

    def _recoverable_failed_review_submission(
        self,
        paper_id: str,
        requested: TeacherReviewSubmission,
    ) -> TeacherReviewSubmission:
        """Resume an equivalent partial review even when the UI issued a new flow."""

        failed = self._m0.get_assessment_run_by_paper(
            paper_id,
            operation="review",
            status="failed",
        )
        if (
            failed is None
            or failed.checkpoint != "state_inputs_frozen"
            or failed.target_audit_id != requested.audit_id
            or failed.target_audit_version != requested.expected_audit_version
            or not failed.operation_id.startswith("review:")
        ):
            return requested
        decision_id = failed.operation_id.removeprefix("review:")
        decision = self._m9.get_review_decision(decision_id)
        if decision is None or not _same_review_outcome(decision, requested):
            return requested
        return TeacherReviewSubmission(
            submission_id=decision.decision_id,
            audit_id=decision.audit_id,
            expected_audit_version=decision.expected_audit_version,
            expected_audit_checksum=decision.expected_audit_checksum,
            reviewer_id=decision.reviewer_id,
            decision=decision.decision,
            final_total_score=decision.final_total_score,
            criterion_overrides=[
                override.model_copy(deep=True)
                for override in decision.criterion_overrides
            ],
            teacher_comment=decision.teacher_comment,
            submitted_at=decision.reviewed_at,
        )

    def rescore(
        self,
        *,
        assessment_submission: AssessmentSubmission,
        audit_id: str,
        expected_rejected_version: int,
        rescore_request_id: str,
        request_id: str,
        index_ref: EvidenceIndexRef,
        knowledge_bundle: KnowledgeBundle,
        state_policy_path: Path,
        teacher_threshold_policy_path: Path,
        class_roster_snapshot: ClassRosterSnapshot | None = None,
    ) -> dict[str, ContractModel]:
        start = self._results.require_start(
            assessment_submission.paper_id,
            learner_id=assessment_submission.learner_id,
        )
        submit = self._results.scored_submit(assessment_submission.paper_id)
        if (
            assessment_submission.content_checksum()
            != submit.request_checksum
        ):
            raise DomainError(
                code="RESCORE_INPUT_MISMATCH",
                module="application",
                message="rescore answers do not match the frozen submission",
                recoverable=True,
            )
        dependencies = capture_assessment_dependencies(
            knowledge_bundle=knowledge_bundle,
            evidence_index_ref=index_ref,
            state_policy_path=state_policy_path,
            teacher_policy_path=teacher_threshold_policy_path,
            class_roster_snapshot=class_roster_snapshot,
        )
        ensure_start_dependencies(start, dependencies)
        operation_id = (
            f"rescore:{assessment_submission.attempt_id}:{audit_id}:"
            f"{expected_rejected_version}:{rescore_request_id}"
        )
        checksum = self._checksum(
            assessment_submission.content_checksum(),
            audit_id,
            str(expected_rejected_version),
            rescore_request_id,
        )
        existing = self._m0.get_assessment_run(operation_id)
        if existing is not None and existing.status == "completed":
            if existing.request_checksum != checksum:
                raise DomainError(
                    code="RESCORE_REQUEST_CONFLICT",
                    module="application",
                    message="rescore request identity does not match",
                    recoverable=False,
                )
            scoring = self._results.exact_scoring(existing)
            require_results(scoring)
            waiting_status = (
                "awaiting_rescore"
                if scoring.has_rejected_score()
                else "awaiting_review"
            )
            submit = self._m0.get_assessment_run(submit.operation_id) or submit
            self._recovery.ensure_waiting(
                submit,
                waiting_status,
                worker_id=f"rescore:{submit.operation_id}",
                lease_seconds=self._lease_seconds,
                scoring_result_checksum=scoring.content_checksum(),
            )
            return {
                "scoring_result": scoring,
                "waiting_status": waiting_status,
            }
        if submit.status != "awaiting_rescore":
            raise DomainError(
                code="RESCORE_NOT_ALLOWED",
                module="application",
                message="model rescore requires a parked rejected attempt",
                recoverable=True,
            )
        run = self._record(
            operation_id=operation_id,
            operation="rescore",
            checksum=checksum,
            start=start,
            attempt_id=assessment_submission.attempt_id,
            target_audit_id=audit_id,
            target_audit_version=expected_rejected_version,
            dependencies=dependencies,
        )
        if run.status == "completed":
            scoring = self._results.exact_scoring(run)
            require_results(scoring)
            waiting_status = (
                "awaiting_rescore"
                if scoring.has_rejected_score()
                else "awaiting_review"
            )
            submit = self._m0.get_assessment_run(submit.operation_id) or submit
            self._recovery.ensure_waiting(
                submit,
                waiting_status,
                worker_id=f"rescore:{submit.operation_id}",
                lease_seconds=self._lease_seconds,
                scoring_result_checksum=scoring.content_checksum(),
            )
            return {
                "scoring_result": scoring,
                "waiting_status": waiting_status,
            }
        run = self._claim(run, request_id)
        try:
            paper = self._results.require_paper(run.paper_id)
            current = self._results.exact_scoring(submit)
            require_results(current)
            rejected = current.get_audit_record(audit_id)
            if (
                rejected.audit_version != expected_rejected_version
                or not rejected.is_rejected()
            ):
                missing("rescore must target the latest rejected audit")
            scoring = self._results.exact_scoring(run)
            if scoring is None and run.scoring_result_checksum is None:
                preparation = self._execute(
                    run,
                    lambda: self._m8.prepare_scoring(
                        assessment_paper=paper,
                        raw_answer_path=assessment_submission,
                        knowledge_bundle=knowledge_bundle,
                    ),
                )
                task = next(
                    (
                        candidate
                        for candidate in preparation.rubric_scoring_tasks
                        if candidate.item_instance.item_instance_id
                        == rejected.item_instance_id
                    ),
                    None,
                )
                if task is None:
                    missing("rejected item has no recoverable rubric task")
                query = preparation.query_for_task(task.scoring_task_id)
                evidence = self._execute(
                    run,
                    lambda query=query: retrieve_for_application(
                        self._m2,
                        query,
                        index_ref,
                        request_id=(
                            f"{run.operation_id}:grading:{query.query_id}"
                        ),
                        policy=self._retrieval_policy,
                    ),
                )
                rubric_result = self._score_constructed_response(
                    run,
                    task,
                    evidence,
                )
                scoring = self._execute(
                    run,
                    lambda: self._m8.apply_model_rescore(
                        current,
                        task,
                        rubric_result,
                        audit_id=audit_id,
                        expected_rejected_version=expected_rejected_version,
                        expected_rejected_checksum=rejected.content_checksum(),
                        expected_raw_answer_checksum=(
                            preparation.raw_answer_checksum
                        ),
                        raw_answer_checksum=preparation.raw_answer_checksum,
                        rescore_request_id=rescore_request_id,
                    ),
                )
            if run.checkpoint == "claimed":
                run = self._advance(
                    run,
                    "scoring_saved",
                    scoring_result_checksum=scoring.content_checksum(),
                )
            if run.checkpoint == "scoring_saved":
                self._complete(run)
            waiting_status = (
                "awaiting_rescore"
                if scoring.has_rejected_score()
                else "awaiting_review"
            )
            submit = self._m0.get_assessment_run(submit.operation_id) or submit
            self._recovery.ensure_waiting(
                submit,
                waiting_status,
                worker_id=f"rescore:{submit.operation_id}",
                lease_seconds=self._lease_seconds,
                scoring_result_checksum=scoring.content_checksum(),
            )
            return {
                "scoring_result": scoring,
                "waiting_status": waiting_status,
            }
        except DomainError as error:
            self._fail(run, error.code)
            raise
        except Exception as error:
            self._fail(run, "WORKFLOW_EXECUTION_FAILED")
            raise self._execution_error("model rescore") from error

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
        dependencies = self._reuse_frozen_roster_capture(
            operation_id,
            dependencies,
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

    def _reuse_frozen_roster_capture(
        self,
        operation_id: str,
        dependencies: AssessmentDependencies,
    ) -> AssessmentDependencies:
        """Keep a retry bound to the first identical roster snapshot."""

        current = self._m0.get_assessment_run(operation_id)
        if (
            current is None
            or current.class_roster_captured_at is None
            or dependencies.class_roster_captured_at is None
            or current.class_roster_size != dependencies.class_roster_size
            or current.class_roster_checksum
            != dependencies.class_roster_checksum
        ):
            return dependencies
        return replace(
            dependencies,
            class_roster_captured_at=current.class_roster_captured_at,
        )

    def _build_observation_batch(
        self,
        scoring: ScoringResultBundle,
    ) -> LearningObservationBatch | None:
        builder = getattr(self._m8, "build_observation_batch", None)
        if not callable(builder):
            return None
        return builder(scoring.paper_id, scoring)

    def frozen_submission(self, attempt_id: str) -> AssessmentSubmission:
        """Load the checksum-bound original student submission for model rescore."""

        runtime_dir = getattr(self._m0, "_runtime_dir", None)
        loader = getattr(self._m0, "load_contract_snapshot", None)
        if runtime_dir is None or not callable(loader):
            raise DomainError(
                code="RESCORE_INPUT_UNAVAILABLE",
                module="application",
                message="frozen original answers are unavailable for model rescore",
                recoverable=True,
            )
        return loader(
            AssessmentSubmission,
            Path(runtime_dir) / "frozen_submissions" / f"{attempt_id}.json",
        )

    def purge_transient_assessment(self, *, paper_id: str, attempt_id: str) -> None:
        """Retain the durable artifacts required by a repeatable result page."""

        # Practice and correction do not update learner/class profiles, but their
        # result pages still display the submitted answers.  Keep both the M8
        # result and its checksum-bound submission; retention/erasure policies
        # remove them through the normal account-governance path.
        del paper_id, attempt_id

    def _persist_frozen_submission(
        self,
        assessment_submission: AssessmentSubmission,
    ) -> None:
        runtime_dir = getattr(self._m0, "_runtime_dir", None)
        saver = getattr(self._m0, "save_contract_snapshot", None)
        if runtime_dir is None or not callable(saver):
            return
        saver(
            assessment_submission,
            Path(runtime_dir)
            / "frozen_submissions"
            / f"{assessment_submission.attempt_id}.json",
        )

    def _claim(self, run: AssessmentRun, worker_id: str) -> AssessmentRun:
        return self._recovery.claim(
            run,
            worker_id,
            lease_seconds=self._lease_seconds,
        )

    def _feedback_for_waiting_commit(
        self,
        run: AssessmentRun,
        *,
        scoring: ScoringResultBundle,
        state: Any,
        index_ref: EvidenceIndexRef | None,
    ):
        existing = self._m7.get_feedback_for_task(run.task_id, run.learner_id)
        if existing is not None:
            return existing
        task = self._results.require_task(run.task_id)
        tutoring = self._execute(
            run,
            lambda: self._m6.decide_next_action(
                task_plan=task,
                scoring_result_bundle=scoring,
                state_update_result=state,
                previous_session_state_snapshot=None,
            ),
        )
        evidence = self._execute(
            run,
            lambda: self._retrieve_feedback_evidence(
                tutoring.evidence_query,
                index_ref,
                request_id=(
                    f"{run.operation_id}:feedback:"
                    f"{tutoring.evidence_query.query_id}"
                ),
            ),
        )
        return self._execute(
            run,
            lambda: self._m7.generate_student_feedback(
                feedback_generation_task=tutoring.feedback_generation_task,
                evidence_bundle=evidence,
            ),
        )

    def _retrieve_feedback_evidence(
        self,
        query: Any,
        index_ref: EvidenceIndexRef | None,
        *,
        request_id: str,
    ) -> Any:
        if index_ref is not None:
            return retrieve_for_application(
                self._m2,
                query,
                index_ref,
                request_id=request_id,
                policy=self._retrieval_policy,
            )
        legacy = getattr(self._m2, "retrieve", None)
        policy = getattr(self._m2, "retrieve_with_policy", None)
        if callable(legacy) and not callable(policy):
            return legacy(
                evidence_query=query,
                evidence_index_ref=None,
            )
        missing("evidence index is unavailable for delayed feedback")

    def _advance(
        self,
        run: AssessmentRun,
        checkpoint: str,
        **refs: object,
    ) -> AssessmentRun:
        return self._recovery.advance(run, checkpoint, **refs)

    @staticmethod
    def _transient_feedback(task: Any, scoring: ScoringResultBundle):
        message = (
            "本次订正已完成；结果只更新待订正状态，不计入学习画像。"
            if task.task_type == "correction"
            else "本次练习已完成；结果仅供即时反馈，不计入学习画像。"
        )
        return StudentFeedbackPackage(
            feedback_id=f"feedback_transient_{task.task_id}",
            task_id=task.task_id,
            learner_id=task.learner_id,
            message=message,
            rubric_feedback=[],
            missing_concept_ids=[],
            evidence_citations=[],
            next_practice_item_ids=[],
            confidence=1.0,
            generated_at=scoring.finalized_at,
        )

    def _complete(self, run: AssessmentRun) -> AssessmentRun:
        return self._recovery.complete(run)

    def _complete_waiting_review(self, run: AssessmentRun) -> AssessmentRun:
        return self._recovery.finish(run)

    def _finish_waiting_submit(
        self,
        submit: AssessmentRun,
        **refs: object,
    ) -> AssessmentRun:
        resumed = self._recovery.resume_parked(
            submit,
            worker_id=f"commit:{submit.operation_id}",
            lease_seconds=self._lease_seconds,
        )
        return self._recovery.finish(resumed, **refs)

    def _fail(self, run: AssessmentRun, code: str) -> None:
        self._recovery.fail(run, code)

    def _score_constructed_response(
        self,
        run: AssessmentRun,
        scoring_task: Any,
        evidence: Any,
    ) -> Any:
        try:
            return self._execute(
                run,
                lambda scoring_task=scoring_task, evidence=evidence: (
                    self._m7.score_subjective_answer(
                        rubric_scoring_task=scoring_task,
                        evidence_bundle=evidence,
                    )
                ),
            )
        except DomainError as error:
            if error.code not in {
                "INVALID_MODEL_JSON",
                "MODEL_ADAPTER_UNCONFIGURED",
                "MODEL_API_UNAVAILABLE",
                "MODEL_INPUT_PRIVACY_BLOCKED",
                "MODEL_OUTPUT_BLOCKED",
            }:
                raise
            deferrer = getattr(self._m8, "defer_rubric_scoring", None)
            if not callable(deferrer):
                raise
            return self._execute(
                run,
                lambda: deferrer(
                    scoring_task,
                    reason_code=error.code,
                ),
            )

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
