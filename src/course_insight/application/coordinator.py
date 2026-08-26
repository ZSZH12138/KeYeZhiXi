"""In-process application coordinator for the public M0-M9 workflows."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from course_insight.contracts.analytics import TeacherAnalyticsBundle
from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.intelligence import (
    ArchitectureScaffoldResult,
    LLMGenerationRequest,
    LLMModelRef,
    RetrievalPolicy,
)
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    LearningModelRun,
    LearningObservationBatch,
)
from course_insight.contracts.platform import (
    ActorContext,
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.contracts.state import StateUpdateResult
from course_insight.infrastructure.json_io import write_json
from course_insight.application.assessment_workflow import AssessmentWorkflow
from course_insight.application.retrieval import retrieve_for_application
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m1_course_governance.service import M1CourseGovernanceService
from course_insight.modules.m2_evidence_retrieval.service import M2EvidenceRetrievalService
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService
from course_insight.modules.m3_knowledge_bundle.teacher_review import TeacherReviewRecord
from course_insight.modules.m4_task_orchestration.service import M4TaskOrchestrationService
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m6_tutoring_fsm.service import M6TutoringControlService
from course_insight.modules.m7_local_model.service import M7LocalModelService
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from course_insight.modules.m8_assessment_scoring.selection_policy import (
    AssessmentSelectionContext,
)
from course_insight.modules.m9_teacher_analytics.service import M9TeacherAnalyticsService


class AppCoordinator:
    """Connect M0-M9 exclusively through their public typed methods."""

    def __init__(
        self,
        m0_service: M0PlatformService,
        m1_service: M1CourseGovernanceService,
        m2_service: M2EvidenceRetrievalService,
        m3_service: M3KnowledgeBundleService,
        m4_service: M4TaskOrchestrationService,
        m5_service: M5StateService,
        m6_service: M6TutoringControlService,
        m7_service: M7LocalModelService,
        m8_service: M8AssessmentService,
        m9_service: M9TeacherAnalyticsService,
        retrieval_policy: RetrievalPolicy | None = None,
    ) -> None:
        self._m0 = m0_service
        self._m1 = m1_service
        self._m2 = m2_service
        self._m3 = m3_service
        self._m4 = m4_service
        self._m5 = m5_service
        self._m6 = m6_service
        self._m7 = m7_service
        self._m8 = m8_service
        self._m9 = m9_service
        self._retrieval_policy = retrieval_policy
        self._assessment_workflow = AssessmentWorkflow(
            m0=m0_service,
            m2=m2_service,
            m4=m4_service,
            m5=m5_service,
            m6=m6_service,
            m7=m7_service,
            m8=m8_service,
            m9=m9_service,
            retrieval_policy=retrieval_policy,
        )

    def start_assessment(
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
        """Create and durably index an assessment without copying its payload."""

        return self._assessment_workflow.start(
            student_text=student_text,
            task_type_hint=task_type_hint,
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            knowledge_bundle=knowledge_bundle,
            selection_context=selection_context,
        )

    def submit_assessment(
        self,
        *,
        assessment_submission: AssessmentSubmission,
        request_id: str,
        index_ref: EvidenceIndexRef,
        knowledge_bundle: KnowledgeBundle,
        state_policy_path: Path,
        teacher_threshold_policy_path: Path,
    ) -> dict[str, ContractModel]:
        """Resume a submission from module-owned results and M0 checkpoints."""

        return self._assessment_workflow.submit(
            assessment_submission=assessment_submission,
            request_id=request_id,
            index_ref=index_ref,
            knowledge_bundle=knowledge_bundle,
            state_policy_path=state_policy_path,
            teacher_threshold_policy_path=teacher_threshold_policy_path,
        )

    def get_pending_assessment(
        self,
        *,
        paper_id: str,
        learner_id: str,
    ) -> dict[str, ContractModel]:
        """Reload one authoritative, not-yet-submitted assessment paper."""

        return self._assessment_workflow.pending_assessment(
            paper_id=paper_id,
            learner_id=learner_id,
        )

    def get_student_assessment(
        self,
        *,
        paper_id: str,
        learner_id: str,
    ) -> dict[str, ContractModel]:
        """Load student-safe authoritative assessment results."""

        return self._assessment_workflow.student_result(
            paper_id=paper_id,
            learner_id=learner_id,
        )

    def get_teacher_review_context(
        self,
        *,
        paper_id: str,
        course_id: str,
        class_id: str,
    ) -> dict[str, ContractModel]:
        """Load scoring, state, and analytics within an exact teacher scope."""

        return self._assessment_workflow.teacher_context(
            paper_id=paper_id,
            course_id=course_id,
            class_id=class_id,
        )

    def apply_suggestion_decision(
        self,
        *,
        report_id: str,
        suggestion_id: str,
        decision: str,
        content: str | None = None,
    ) -> TeacherAnalyticsBundle:
        """Record a teacher decision without auto-scheduling instruction."""

        return self._m9.apply_suggestion_decision(
            report_id=report_id,
            suggestion_id=suggestion_id,
            decision=decision,
            content=content,
        )

    def review_assessment(
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
    ) -> dict[str, ContractModel]:
        """Resume a teacher review without applying an existing audit twice."""

        return self._assessment_workflow.review(
            paper_id=paper_id,
            review_submission=review_submission,
            request_id=request_id,
            knowledge_bundle=knowledge_bundle,
            state_policy_path=state_policy_path,
            teacher_threshold_policy_path=teacher_threshold_policy_path,
            course_id=course_id,
            class_id=class_id,
            index_ref=index_ref,
        )

    def rescore_assessment(
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
    ) -> dict[str, ContractModel]:
        """Replay a bound model rescore without posting pending scores."""

        return self._assessment_workflow.rescore(
            assessment_submission=assessment_submission,
            audit_id=audit_id,
            expected_rejected_version=expected_rejected_version,
            rescore_request_id=rescore_request_id,
            request_id=request_id,
            index_ref=index_ref,
            knowledge_bundle=knowledge_bundle,
            state_policy_path=state_policy_path,
            teacher_threshold_policy_path=teacher_threshold_policy_path,
        )

    def frozen_assessment_submission(
        self,
        attempt_id: str,
    ) -> AssessmentSubmission:
        """Return the frozen original answers bound to one attempt."""

        return self._assessment_workflow.frozen_submission(attempt_id)

    def initialize_course(
        self,
        *,
        raw_course_files: list[Path],
        course_metadata_path: Path,
        source_authorization_path: Path | None,
        output_dir: Path,
        concept_seed_path: Path,
        item_seed_path: Path,
        rubric_seed_path: Path,
        blueprint_seed_path: Path,
        prerequisite_seed_path: Path | None,
        misconception_seed_path: Path | None,
        teacher_review_id: str | None = None,
        teacher_review_version: int | None = None,
    ) -> dict[str, ContractModel]:
        """Produce steps 1-3 from governed course files and teacher seeds."""

        if (teacher_review_id is None) != (teacher_review_version is None):
            raise DomainError(
                code="M3_REVIEW_INPUT_INVALID",
                module="application",
                message="teacher review id and version must be supplied together",
                recoverable=True,
            )
        self._m0.initialize()
        course_package = self._m1.import_course(
            raw_course_files=raw_course_files,
            course_metadata_path=course_metadata_path,
            source_authorization_path=source_authorization_path,
            output_dir=output_dir,
        )
        if self._retrieval_policy is not None and self._retrieval_policy.strategy in {
            "vector",
            "hybrid",
        }:
            build_vector_index = getattr(self._m2, "build_vector_index", None)
            if not callable(build_vector_index):
                raise DomainError(
                    code="VECTOR_INDEX_BUILD_UNAVAILABLE",
                    module="application",
                    message="configured retrieval strategy cannot build a vector index",
                    recoverable=True,
                )
            index_ref = build_vector_index(course_package=course_package)
        else:
            index_ref = self._m2.build_index(course_package=course_package)
        if teacher_review_id is None:
            knowledge_bundle = self._m3.build_knowledge_bundle(
                course_package=course_package,
                concept_seed_path=concept_seed_path,
                item_seed_path=item_seed_path,
                rubric_seed_path=rubric_seed_path,
                blueprint_seed_path=blueprint_seed_path,
                prerequisite_seed_path=prerequisite_seed_path,
                misconception_seed_path=misconception_seed_path,
            )
        else:
            knowledge_bundle = self._m3.build_knowledge_bundle_after_approval(
                review_id=teacher_review_id,
                review_version=teacher_review_version,
                course_package=course_package,
                concept_seed_path=concept_seed_path,
                item_seed_path=item_seed_path,
                rubric_seed_path=rubric_seed_path,
                blueprint_seed_path=blueprint_seed_path,
                prerequisite_seed_path=prerequisite_seed_path,
                misconception_seed_path=misconception_seed_path,
            )
        return {
            "course_package": course_package,
            "index_ref": index_ref,
            "knowledge_bundle": knowledge_bundle,
        }

    def create_knowledge_review_draft(
        self,
        *,
        review_id: str,
        subject_id: str,
        validation_report_ref: str,
        now: datetime,
        concept_seed_path: Path,
        item_seed_path: Path,
        rubric_seed_path: Path,
        blueprint_seed_path: Path,
        prerequisite_seed_path: Path | None = None,
        misconception_seed_path: Path | None = None,
    ) -> TeacherReviewRecord:
        """Start the M3 S4 review against a captured seed checksum."""

        return self._m3.create_teacher_review_draft(
            review_id=review_id,
            subject_id=subject_id,
            validation_report_ref=validation_report_ref,
            now=now,
            concept_seed_path=concept_seed_path,
            item_seed_path=item_seed_path,
            rubric_seed_path=rubric_seed_path,
            blueprint_seed_path=blueprint_seed_path,
            prerequisite_seed_path=prerequisite_seed_path,
            misconception_seed_path=misconception_seed_path,
        )

    def get_knowledge_review(self, review_id: str) -> TeacherReviewRecord:
        """Read one M3 knowledge-package review for the teacher Web flow."""

        return self._m3.get_teacher_review(review_id)

    def submit_knowledge_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Submit one M3 S4 review through its CAS transition."""

        return self._m3.submit_teacher_review(
            review_id, reviewer_pseudonym, reason, expected_version, now
        )

    def approve_knowledge_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Approve one M3 S4 review through its CAS transition."""

        return self._m3.approve_teacher_review(
            review_id, reviewer_pseudonym, reason, expected_version, now
        )

    def reject_knowledge_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Reject one M3 S4 review through its CAS transition."""

        return self._m3.reject_teacher_review(
            review_id, reviewer_pseudonym, reason, expected_version, now
        )

    def recall_knowledge_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Recall one published M3 S4 review through its CAS transition."""

        return self._m3.recall_teacher_review(
            review_id, reviewer_pseudonym, reason, expected_version, now
        )

    def run_assessment_cycle(
        self,
        *,
        index_ref: EvidenceIndexRef,
        knowledge_bundle: KnowledgeBundle,
        student_text: str,
        task_type_hint: str | None,
        course_id: str,
        class_id: str,
        learner_id: str,
        session_id: str,
        raw_answer_path: Path | AssessmentSubmission,
        state_policy_path: Path,
        teacher_threshold_policy_path: Path,
    ) -> dict[str, ContractModel]:
        """Produce steps 4-13 using direct upstream contract objects."""

        task_plan = self._m4.create_task_plan(
            student_text=student_text,
            task_type_hint=task_type_hint,
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
            session_id=session_id,
            knowledge_bundle=knowledge_bundle,
            learner_state_snapshot=None,
        )
        paper = self._m8.generate_paper(
            task_plan=task_plan,
            knowledge_bundle=knowledge_bundle,
            learner_state_snapshot=None,
            diagnosis_result=None,
        )
        preparation = self._m8.prepare_scoring(
            assessment_paper=paper,
            raw_answer_path=raw_answer_path,
            knowledge_bundle=knowledge_bundle,
        )
        if len(preparation.rubric_scoring_tasks) != 1:
            raise DomainError(
                code="ASSESSMENT_FLOW_INVALID",
                module="application",
                message="the current scoring flow requires one subjective scoring task",
            )
        scoring_task = preparation.rubric_scoring_tasks[0]
        scoring_query = preparation.query_for_task(scoring_task.scoring_task_id)
        scoring_evidence = retrieve_for_application(
            self._m2,
            scoring_query,
            index_ref,
            request_id=f"assessment:{scoring_query.query_id}:grading",
            policy=self._retrieval_policy,
        )
        first_result = self._m7.score_subjective_answer(
            rubric_scoring_task=scoring_task,
            evidence_bundle=scoring_evidence,
        )
        second_result = self._m7.score_subjective_answer(
            rubric_scoring_task=scoring_task,
            evidence_bundle=scoring_evidence,
        )
        merger = getattr(self._m8, "merge_independent_rubric_results", None)
        rubric_result = (
            merger(scoring_task, first_result, second_result)
            if callable(merger)
            else first_result
        )
        scoring = self._m8.finalize_scoring(
            scoring_preparation_result=preparation,
            rubric_scoring_results=[rubric_result],
        )
        if scoring.requires_teacher_review() or scoring.has_rejected_score():
            result: dict[str, ContractModel | str] = {
                "task_plan": task_plan,
                "assessment_paper": paper,
                "scoring_preparation": preparation,
                "scoring_evidence": scoring_evidence,
                "rubric_scoring_result": rubric_result,
                "scoring_result": scoring,
                "waiting_status": (
                    "awaiting_rescore"
                    if scoring.has_rejected_score()
                    else "awaiting_review"
                ),
            }
            return result  # type: ignore[return-value]
        self._m0.append_learning_events(events=scoring.learning_events)
        observation_batch = self._build_observation_batch(scoring)
        learning_model_run = (
            None
            if observation_batch is None
            else self._run_learning_models(observation_batch, knowledge_bundle)
        )
        state = self._m5.update_state(
            scoring_result_bundle=scoring,
            knowledge_bundle=knowledge_bundle,
            previous_learner_state_snapshot=None,
            previous_class_state_snapshot=None,
            state_policy_path=state_policy_path,
            learning_observation_batch=observation_batch,
            learning_model_run=learning_model_run,
        )
        tutoring = self._m6.decide_next_action(
            task_plan=task_plan,
            scoring_result_bundle=scoring,
            state_update_result=state,
            previous_session_state_snapshot=None,
        )
        feedback_evidence = retrieve_for_application(
            self._m2,
            tutoring.evidence_query,
            index_ref,
            request_id=f"assessment:{tutoring.evidence_query.query_id}:feedback",
            policy=self._retrieval_policy,
        )
        feedback = self._m7.generate_student_feedback(
            feedback_generation_task=tutoring.feedback_generation_task,
            evidence_bundle=feedback_evidence,
        )
        analytics = self._m9.build_teacher_analytics(
            knowledge_bundle=knowledge_bundle,
            scoring_result_bundle=scoring,
            state_update_result=state,
            teacher_threshold_policy_path=teacher_threshold_policy_path,
        )
        result: dict[str, ContractModel] = {
            "task_plan": task_plan,
            "assessment_paper": paper,
            "scoring_preparation": preparation,
            "scoring_evidence": scoring_evidence,
            "rubric_scoring_result": rubric_result,
            "scoring_result": scoring,
            "state_result": state,
            "tutoring_result": tutoring,
            "feedback": feedback,
            "analytics": analytics,
        }
        if learning_model_run is not None:
            result["learning_model_run"] = learning_model_run
        return result

    def run_teacher_review_cycle(
        self,
        *,
        knowledge_bundle: KnowledgeBundle,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        raw_review_path: Path | TeacherReviewSubmission,
        state_policy_path: Path,
        teacher_threshold_policy_path: Path,
    ) -> dict[str, ContractModel]:
        """Produce steps 14-17 without mutating the supplied scoring bundle."""

        decision = self._m9.record_teacher_review(
            raw_review_path=raw_review_path,
            current_scoring_result_bundle=scoring_result_bundle,
        )
        reviewed = self._m8.apply_teacher_review(
            current_scoring_result_bundle=scoring_result_bundle,
            teacher_review_decision=decision,
        )
        self._m0.append_learning_events(events=reviewed.learning_events)
        rejected = reviewed.has_rejected_score()
        observation_batch = (
            None if rejected else self._build_observation_batch(reviewed)
        )
        learning_model_run = (
            None
            if observation_batch is None
            else self._run_learning_models(observation_batch, knowledge_bundle)
        )
        recomputed = (
            state_update_result.model_copy(deep=True)
            if rejected
            else self._m5.update_state(
                scoring_result_bundle=reviewed,
                knowledge_bundle=knowledge_bundle,
                previous_learner_state_snapshot=(
                    state_update_result.learner_state_snapshot
                ),
                previous_class_state_snapshot=(
                    state_update_result.class_state_snapshot
                ),
                state_policy_path=state_policy_path,
                learning_observation_batch=observation_batch,
                learning_model_run=learning_model_run,
            )
        )
        refreshed = self._m9.build_teacher_analytics(
            knowledge_bundle=knowledge_bundle,
            scoring_result_bundle=reviewed,
            state_update_result=recomputed,
            teacher_threshold_policy_path=teacher_threshold_policy_path,
        )
        result: dict[str, ContractModel] = {
            "review_decision": decision,
            "reviewed_scoring_result": reviewed,
            "recomputed_state_result": recomputed,
            "refreshed_analytics": refreshed,
        }
        if learning_model_run is not None:
            result["learning_model_run"] = learning_model_run
        return result

    def _build_observation_batch(
        self,
        scoring: ScoringResultBundle,
    ) -> LearningObservationBatch | None:
        builder = getattr(self._m8, "build_observation_batch", None)
        if not callable(builder):
            return None
        return builder(scoring.paper_id, scoring)

    def _run_learning_models(
        self,
        observation_batch: LearningObservationBatch,
        knowledge_bundle: KnowledgeBundle,
    ) -> LearningModelRun | None:
        """Forward authoritative M8 evidence without constructing an empty batch."""

        runner = getattr(self._m5, "run_learning_models", None)
        if not callable(runner):
            return None
        return runner(observation_batch, knowledge_bundle)

    def run_intelligence_architecture(
        self,
        *,
        actor_context: ActorContext,
        course_package_id: str,
        learner_id: str,
        requested_at: datetime,
    ) -> ArchitectureScaffoldResult:
        """Wire the v2 capability boundaries into one explicit empty result.

        原始输入：M0 的 ActorContext、课程包标识、伪匿名学习者标识和请求时间。
        契约来源：platform、intelligence 与 learning_models 公共契约。
        返回消费者：后续 M0 系统外层与各能力适配器。
        业务校验：不启动 Django、不连接 pgvector、不读取密钥、不调用 DeepSeek，
        也不估计学习模型参数；所有分支必须组合为显式空结果。
        错误码：ARCHITECTURE_SCAFFOLD_NOT_EMPTY（任一组件伪造非空结果时）。
        """

        web_job_status = self._m0.prepare_django_frontend(
            actor_context=actor_context,
            requested_at=requested_at,
        )
        vector_index = self._m2.initialize_vector_store(
            course_package_id=course_package_id,
            requested_at=requested_at,
        )
        retrieval_audit = self._m2.empty_retrieval_audit(
            index_ref=vector_index,
            requested_at=requested_at,
        )
        model_ref = LLMModelRef(
            provider="deepseek",
            model_name="configured-at-runtime",
            model_version="unconfigured",
            api_key_env="DEEPSEEK_API_KEY",
            status="empty",
        )
        scoring_generation = self._m7.invoke_deepseek(
            request=LLMGenerationRequest(
                request_id=f"scoring_empty_{learner_id}",
                use_case="rubric_scoring",
                model_ref=model_ref,
                prompt_template_id="m7_scoring_unconfigured",
                prompt_template_version="unconfigured",
                evidence_ids=[],
                input_checksum="empty",
                created_at=requested_at,
            )
        )
        observation_batch = LearningObservationBatch(
            batch_id=f"observations_empty_{learner_id}",
            learner_id=learner_id,
            observations=[],
            watermark="empty",
            created_at=requested_at,
        )
        learning_model_run = self._m5.run_learning_models(
            observation_batch=observation_batch,
        )
        calibration_result = self._m8.calibrate_irt(
            observation_batch=observation_batch,
            requested_at=requested_at,
        )
        adaptive_policy = AdaptiveSelectionPolicy(
            policy_id=f"adaptive_empty_{course_package_id}",
            version="unconfigured",
            parameter_set_id=None,
            max_items=1,
            concept_quotas={},
            status="empty",
        )
        adaptive_selection_result = self._m8.select_adaptive_items(
            policy=adaptive_policy,
            ability_estimate=AbilityEstimate(
                estimate_id=f"ability_empty_{learner_id}",
                learner_id=learner_id,
                parameter_set_id=(
                    calibration_result.parameter_set.parameter_set_id
                ),
                theta=None,
                standard_error=None,
                status="empty",
                estimated_at=requested_at,
            ),
            requested_at=requested_at,
        )
        teacher_generation = self._m9.generate_teacher_narrative(
            request=LLMGenerationRequest(
                request_id=f"teacher_empty_{learner_id}",
                use_case="teacher_narrative",
                model_ref=model_ref,
                prompt_template_id="m9_teacher_unconfigured",
                prompt_template_version="unconfigured",
                evidence_ids=[],
                input_checksum="empty",
                created_at=requested_at,
            )
        )
        model_quality_report = self._m9.build_model_quality_report(
            calibration_result=calibration_result,
            requested_at=requested_at,
        )
        return ArchitectureScaffoldResult(
            status="empty",
            web_job_status=web_job_status,
            vector_index=vector_index,
            retrieval_audit=retrieval_audit,
            scoring_generation=scoring_generation,
            teacher_generation=teacher_generation,
            learning_model_run=learning_model_run,
            calibration_result=calibration_result,
            adaptive_selection_result=adaptive_selection_result,
            model_quality_report=model_quality_report,
            completed_at=requested_at,
        )

    def export_run_manifest(
        self,
        *,
        objects: list[ContractModel],
        output_path: Path,
    ) -> Path:
        """Export only contract identity, checksum, and timestamp metadata."""

        records = [self._manifest_record(obj) for obj in objects]
        timestamps = [record["timestamp"] for record in records]
        write_json(
            output_path,
            {
                "generated_at": max(
                    timestamps,
                    default="1970-01-01T00:00:00+00:00",
                ),
                "objects": records,
            },
        )
        return output_path

    @staticmethod
    def _manifest_record(obj: ContractModel) -> dict[str, str]:
        payload = obj.model_dump(mode="python")
        identity = next(
            (
                value
                for key, value in payload.items()
                if key.endswith("_id") and isinstance(value, str)
            ),
            obj.content_checksum()[:16],
        )
        timestamp = next(
            (
                value.isoformat()
                for value in payload.values()
                if isinstance(value, datetime)
            ),
            "1970-01-01T00:00:00+00:00",
        )
        return {
            "checksum": obj.content_checksum(),
            "id": identity,
            "timestamp": timestamp,
        }
