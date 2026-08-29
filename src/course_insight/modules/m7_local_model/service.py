"""Formal M7 local-model service boundary."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from course_insight.contracts.assessment import (
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceBundle
from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMGenerationResult,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.tutoring import (
    EvidenceCitation,
    FeedbackGenerationTask,
    STUDENT_CITATION_QUOTE_PLACEHOLDER,
    StudentFeedbackPackage,
)
from course_insight.infrastructure.deepseek import EmptyDeepSeekAdapter
from course_insight.modules.m7_local_model.adapter import (
    GovernedRubricScoringAdapter,
    M7InvocationFailure,
    M7ScoringOutcome,
    RubricScoringAdapter,
)
from course_insight.modules.m7_local_model.prompts import (
    FEEDBACK_PROMPT_ID,
    FEEDBACK_PROMPT_VERSION,
    SCORING_PROMPT_ID,
    feedback_message,
)
from course_insight.modules.m7_local_model.repository import (
    M7ModelAuditRecord,
    M7Repository,
)


M7_AUDIT_RETENTION_DAYS = 180


@runtime_checkable
class ModelOutputValidator(Protocol):
    """Compatibility boundary for the original validator object shape."""

    def validate(self, result: RubricScoringResult) -> bool:
        """Return whether one already structured score is acceptable."""


class M7LocalModelService:
    """Validate model scoring and build deterministic learner feedback."""

    def __init__(
        self,
        local_model_adapter: Any,
        prompt_repository: M7Repository,
        output_validator: Any,
    ) -> None:
        self._local_model_adapter = local_model_adapter
        self._prompt_repository = prompt_repository
        self._output_validator = output_validator

    def invoke_deepseek(
        self,
        request: LLMGenerationRequest,
    ) -> LLMGenerationResult:
        """Return the governed DeepSeek placeholder without an API call.

        原始输入：M7 量规评分或学生反馈的 LLMGenerationRequest。
        契约来源：intelligence 中的 DeepSeek 请求与结果契约。
        返回消费者：AppCoordinator 和后续 M7 真实适配器。
        业务校验：不读取密钥、不访问网络且不生成文本或引用。
        错误码：LLM_USE_CASE_INVALID；合法请求固定返回 empty。
        """

        if request.use_case not in {"rubric_scoring", "student_feedback"}:
            raise DomainError(
                code="LLM_USE_CASE_INVALID",
                module="m7",
                message="M7 accepts only scoring and student-feedback generation",
            )
        return EmptyDeepSeekAdapter().generate(request)

    def score_subjective_answer(
        self,
        rubric_scoring_task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> RubricScoringResult:
        """Score one subjective answer against its frozen rubric.

        原始输入：M8 量规评分任务和 M2 课程证据包。
        契约来源：prepare_scoring 与 retrieve 的对齐输出。
        返回消费者：M8.finalize_scoring。
        业务校验：查询、量规分项、学生证据、课程引用和总分必须一致。
        错误码：EVIDENCE_REQUIRED、MODEL_ADAPTER_UNCONFIGURED、
        MODEL_API_UNAVAILABLE、MODEL_OUTPUT_BLOCKED、INVALID_MODEL_JSON。
        """

        self._require_aligned_evidence(
            rubric_scoring_task.evidence_query_id,
            evidence_bundle,
        )
        available_ids = set(evidence_bundle.citation_ids())
        for criterion in rubric_scoring_task.rubric.criteria:
            if not set(criterion.course_evidence_ids).intersection(available_ids):
                raise DomainError(
                    code="EVIDENCE_REQUIRED",
                    module="m7",
                    message="rubric evidence is absent from the retrieved bundle",
                    details={"criterion_id": criterion.criterion_id},
                    recoverable=True,
                )
        outcome: M7ScoringOutcome | None = None
        if isinstance(
            self._local_model_adapter,
            GovernedRubricScoringAdapter,
        ):
            try:
                outcome = self._local_model_adapter.score_governed(
                    rubric_scoring_task,
                    evidence_bundle,
                )
            except M7InvocationFailure as failure:
                self._record_failed_invocation(failure)
                raise failure.error from None
            if not isinstance(outcome, M7ScoringOutcome):
                raise DomainError(
                    code="INVALID_MODEL_JSON",
                    module="m7",
                    message="the governed scorer returned an invalid outcome",
                    details={
                        "scoring_task_id": (
                            rubric_scoring_task.scoring_task_id
                        )
                    },
                    recoverable=True,
                )
            result = outcome.result
        else:
            if not isinstance(self._local_model_adapter, RubricScoringAdapter):
                raise DomainError(
                    code="INVALID_MODEL_JSON",
                    module="m7",
                    message="the configured rubric adapter cannot score answers",
                    details={
                        "scoring_task_id": (
                            rubric_scoring_task.scoring_task_id
                        )
                    },
                    recoverable=True,
                )
            result = self._local_model_adapter.score(
                rubric_scoring_task,
                evidence_bundle,
            )
        try:
            self._validate_scoring_result(
                rubric_scoring_task,
                evidence_bundle,
                result,
                student_answer=(
                    rubric_scoring_task.student_answer
                    if outcome is None
                    else outcome.student_answer_for_validation
                ),
                require_teacher_review=(outcome is None),
                expected_review_flags=(
                    None
                    if outcome is None
                    else outcome.review_selection.review_flags
                ),
            )
        except DomainError:
            if outcome is not None:
                self._record_rejected_invocation(outcome)
            raise
        if outcome is not None:
            self._record_successful_invocation(outcome)
        else:
            self._save_prompt_record(
                SCORING_PROMPT_ID,
                {
                    "scoring_task_id": rubric_scoring_task.scoring_task_id,
                    "evidence_query_id": evidence_bundle.query_id,
                    "adapter": result.model_name,
                },
            )
        return result

    def generate_student_feedback(
        self,
        feedback_generation_task: FeedbackGenerationTask,
        evidence_bundle: EvidenceBundle,
    ) -> StudentFeedbackPackage:
        """Build cited learner-safe feedback without a model call.

        原始输入：M6 反馈生成任务和 M2 课程证据包。
        契约来源：decide_next_action 与 retrieve 的对齐输出。
        返回消费者：学生端反馈流程。
        业务校验：查询一致、引用可定位且不得泄露最终答案。
        错误码：EVIDENCE_REQUIRED。
        """

        self._require_aligned_evidence(
            feedback_generation_task.evidence_query_id,
            evidence_bundle,
        )
        citations = [
            EvidenceCitation(
                evidence_id=chunk.evidence_id,
                source_id=chunk.source_id,
                locator=chunk.locator,
                quote=STUDENT_CITATION_QUOTE_PLACEHOLDER,
            )
            for chunk in evidence_bundle.evidence_chunks
        ]
        target_ids = feedback_generation_task.target_concept_ids()
        package = StudentFeedbackPackage(
            feedback_id=f"feedback_{feedback_generation_task.feedback_task_id}",
            task_id=feedback_generation_task.task_id,
            learner_id=feedback_generation_task.learner_id,
            message=feedback_message(
                target_ids,
                feedback_generation_task.teaching_action.action_type,
            ),
            rubric_feedback=[],
            missing_concept_ids=target_ids,
            evidence_citations=citations,
            next_practice_item_ids=[
                f"practice_{concept_id}" for concept_id in target_ids
            ],
            confidence=0.6,
            generated_at=feedback_generation_task.created_at,
        )
        self._validate_feedback_package(
            feedback_generation_task,
            evidence_bundle,
            package,
        )
        self._save_prompt_record(
            FEEDBACK_PROMPT_ID,
            {
                "feedback_task_id": (
                    feedback_generation_task.feedback_task_id
                ),
                "evidence_query_id": evidence_bundle.query_id,
                "prompt_template_version": FEEDBACK_PROMPT_VERSION,
                "generation_mode": "deterministic",
                "action_type": (
                    feedback_generation_task.teaching_action.action_type
                ),
                "must_hide_answer": feedback_generation_task.must_hide_answer(),
            },
        )
        insert_or_get = getattr(
            self._prompt_repository,
            "insert_or_get_feedback",
            None,
        )
        if callable(insert_or_get):
            authoritative = insert_or_get(package.model_copy(deep=True))
            if authoritative != package:
                raise RuntimeError("M7 persisted feedback conflicts with result")
            return authoritative.model_copy(deep=True)
        feedback_saver = getattr(
            self._prompt_repository,
            "save_feedback",
            None,
        )
        if callable(feedback_saver):
            feedback_saver(package.model_copy(deep=True))
        return package

    def save_transient_feedback(
        self,
        package: StudentFeedbackPackage,
    ) -> StudentFeedbackPackage:
        """Persist learner-safe feedback that must not enter M5/M9.

        Practice and correction results still need a reloadable result page, but
        they are deliberately excluded from the learner/class profile pipeline.
        """

        package.validate_business_rules()
        self._require_student_safe_feedback(package)
        insert_or_get = getattr(
            self._prompt_repository,
            "insert_or_get_feedback",
            None,
        )
        if callable(insert_or_get):
            authoritative = insert_or_get(package.model_copy(deep=True))
            if authoritative != package:
                raise RuntimeError("M7 persisted feedback conflicts with result")
            return authoritative.model_copy(deep=True)
        feedback_saver = getattr(
            self._prompt_repository,
            "save_feedback",
            None,
        )
        if callable(feedback_saver):
            feedback_saver(package.model_copy(deep=True))
        return package.model_copy(deep=True)

    def get_feedback(
        self,
        feedback_id: str,
    ) -> StudentFeedbackPackage | None:
        """Recover feedback by its stable identity."""

        getter = getattr(self._prompt_repository, "get_feedback", None)
        if not callable(getter):
            return None
        package = getter(feedback_id)
        if package is None:
            return None
        self._require_student_safe_feedback(package)
        return package.model_copy(deep=True)

    def get_feedback_for_task(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        """Recover the feedback package for one task and learner."""

        getter = getattr(
            self._prompt_repository,
            "get_feedback_for_task",
            None,
        )
        if not callable(getter):
            getter = getattr(
                self._prompt_repository,
                "get_feedback_by_task_and_learner",
                None,
            )
        if not callable(getter):
            return None
        package = getter(task_id, learner_id)
        if package is None:
            return None
        self._require_student_safe_feedback(package)
        return package.model_copy(deep=True)

    def get_feedback_by_task_and_learner(
        self,
        task_id: str,
        learner_id: str,
    ) -> StudentFeedbackPackage | None:
        return self.get_feedback_for_task(task_id, learner_id)

    def get_model_audit_for_admin(
        self,
        invocation_id: str,
        *,
        requester_role: str,
    ) -> M7ModelAuditRecord | None:
        """Return one privacy-minimized audit only to a system administrator."""

        self._require_system_admin(requester_role)
        getter = getattr(self._prompt_repository, "get_execution_audit", None)
        if not callable(getter):
            raise DomainError(
                code="MODEL_AUDIT_PERSISTENCE_REQUIRED",
                module="m7",
                message="the configured M7 repository cannot read model audits",
            )
        record = getter(invocation_id)
        return record

    def purge_expired_model_audits(
        self,
        *,
        now: datetime,
        requester_role: str,
        retention_days: int = M7_AUDIT_RETENTION_DAYS,
    ) -> int:
        """Apply the administrator-approved 180-day retention default."""

        self._require_system_admin(requester_role)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("M7 audit purge time must be timezone-aware")
        if type(retention_days) is not int or not 1 <= retention_days <= 3650:
            raise ValueError("M7 audit retention days must be between 1 and 3650")
        purger = getattr(
            self._prompt_repository,
            "purge_execution_audits_before",
            None,
        )
        if not callable(purger):
            raise DomainError(
                code="MODEL_AUDIT_PERSISTENCE_REQUIRED",
                module="m7",
                message="the configured M7 repository cannot purge model audits",
            )
        return int(purger(now - timedelta(days=retention_days)))

    @staticmethod
    def _require_aligned_evidence(
        expected_query_id: str,
        evidence_bundle: EvidenceBundle,
    ) -> None:
        if (
            evidence_bundle.query_id != expected_query_id
            or evidence_bundle.is_empty()
        ):
            raise DomainError(
                code="EVIDENCE_REQUIRED",
                module="m7",
                message="the evidence bundle must match the task query and be nonempty",
                details={"expected_query_id": expected_query_id},
                recoverable=True,
            )

    def _validate_scoring_result(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
        result: Any,
        *,
        student_answer: str,
        require_teacher_review: bool,
        expected_review_flags: tuple[str, ...] | None,
    ) -> None:
        if not isinstance(result, RubricScoringResult):
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="the rubric adapter returned an invalid result type",
                details={"scoring_task_id": task.scoring_task_id},
                recoverable=True,
            )
        if (
            result.scoring_task_id != task.scoring_task_id
            or len(result.criterion_scores)
            != len(task.rubric.criteria)
            or {
                score.criterion_id for score in result.criterion_scores
            }
            != task.criterion_ids()
            or result.total_score > task.max_score() + 1e-9
            or (
                require_teacher_review
                and "teacher_review_required" not in result.review_flags
            )
            or (
                expected_review_flags is not None
                and tuple(result.review_flags) != expected_review_flags
            )
        ):
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="adapter output does not match the frozen scoring task",
                details={"scoring_task_id": task.scoring_task_id},
                recoverable=True,
            )
        available_ids = set(evidence_bundle.citation_ids())
        for score in result.criterion_scores:
            criterion = task.rubric.criterion(score.criterion_id)
            if (
                score.score > criterion.max_score + 1e-9
                or (
                    score.student_evidence
                    and score.student_evidence not in student_answer
                )
                or (
                    score.score > 0.0
                    and task.rubric.review_policy.require_evidence_for_positive_score
                    and score.course_evidence_id is None
                )
            ):
                raise DomainError(
                    code="INVALID_MODEL_JSON",
                    module="m7",
                    message="adapter criterion output violates the frozen rubric",
                    details={"criterion_id": score.criterion_id},
                    recoverable=True,
                )
            if score.course_evidence_id is not None and (
                score.course_evidence_id not in available_ids
                or score.course_evidence_id not in criterion.course_evidence_ids
            ):
                raise DomainError(
                    code="EVIDENCE_REQUIRED",
                    module="m7",
                    message="adapter output cites evidence outside the bundle",
                    details={"criterion_id": score.criterion_id},
                    recoverable=True,
                )
        if isinstance(self._output_validator, ModelOutputValidator):
            accepted = self._output_validator.validate(result)
        elif callable(self._output_validator):
            accepted = self._output_validator(result)
        else:
            accepted = True
        if accepted is False:
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="the configured output validator rejected the result",
                details={"scoring_task_id": task.scoring_task_id},
                recoverable=True,
            )

    @staticmethod
    def _validate_feedback_package(
        task: FeedbackGenerationTask,
        evidence_bundle: EvidenceBundle,
        package: Any,
    ) -> None:
        if not isinstance(package, StudentFeedbackPackage):
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="the feedback builder returned an invalid result type",
                details={"feedback_task_id": task.feedback_task_id},
                recoverable=True,
            )
        citation_ids = set(package.citation_ids())
        evidence_by_id = {
            chunk.evidence_id: chunk for chunk in evidence_bundle.evidence_chunks
        }
        citation_locations_match = all(
            citation.evidence_id in evidence_by_id
            and citation.source_id
            == evidence_by_id[citation.evidence_id].source_id
            and citation.locator == evidence_by_id[citation.evidence_id].locator
            for citation in package.evidence_citations
        )
        if (
            package.feedback_id != f"feedback_{task.feedback_task_id}"
            or package.task_id != task.task_id
            or package.learner_id != task.learner_id
            or not citation_ids
            or not citation_ids <= set(evidence_bundle.citation_ids())
            or not citation_locations_match
            or not set(package.missing_concept_ids)
            <= set(task.target_concept_ids())
            or not package.safe_for_student()
        ):
            raise DomainError(
                code="EVIDENCE_REQUIRED",
                module="m7",
                message="student feedback must remain aligned, cited, and answer-safe",
                details={"feedback_task_id": task.feedback_task_id},
                recoverable=True,
            )

    def _record_successful_invocation(
        self,
        outcome: M7ScoringOutcome,
    ) -> None:
        self._record_execution_audit(
            prompt_record=outcome.prompt_record,
            invocation=outcome.audit,
            safety=outcome.safety,
            result=outcome.result,
        )

    def _record_failed_invocation(
        self,
        failure: M7InvocationFailure,
    ) -> None:
        self._record_execution_audit(
            prompt_record=failure.prompt_record,
            invocation=failure.audit,
            safety=failure.safety,
            result=None,
        )

    def _record_rejected_invocation(
        self,
        outcome: M7ScoringOutcome,
    ) -> None:
        """Record a completed call rejected by the service-level validator."""

        rejected_safety = outcome.safety.model_copy(
            update={
                "status": "blocked",
                "flags": list(
                    dict.fromkeys(
                        [*outcome.safety.flags, "invalid_model_output"]
                    )
                ),
            },
            deep=True,
        )
        self._record_execution_audit(
            prompt_record=outcome.prompt_record,
            invocation=outcome.audit,
            safety=rejected_safety,
            result=None,
        )

    def _record_execution_audit(
        self,
        *,
        prompt_record: dict[str, Any],
        invocation: ModelInvocationAudit,
        safety: SafetyCheckResult,
        result: RubricScoringResult | None,
    ) -> None:
        saver = getattr(
            self._prompt_repository,
            "save_execution_audit",
            None,
        )
        if not callable(saver):
            raise DomainError(
                code="MODEL_AUDIT_PERSISTENCE_REQUIRED",
                module="m7",
                message=(
                    "governed model scoring requires durable atomic audit storage"
                ),
            )
        saver(
            M7ModelAuditRecord.from_artifacts(
                prompt_record=dict(prompt_record),
                invocation=invocation,
                safety=safety,
                result=result,
            )
        )

    @staticmethod
    def _require_student_safe_feedback(package: Any) -> None:
        if (
            not isinstance(package, StudentFeedbackPackage)
            or not package.safe_for_student()
        ):
            raise DomainError(
                code="MODEL_OUTPUT_BLOCKED",
                module="m7",
                message="persisted student feedback failed the learner safety gate",
                recoverable=True,
            )

    @staticmethod
    def _require_system_admin(requester_role: str) -> None:
        if requester_role != "system_admin":
            raise DomainError(
                code="MODEL_AUDIT_ACCESS_DENIED",
                module="m7",
                message="M7 model audits are restricted to system administrators",
            )

    def _save_prompt_record(
        self,
        prompt_id: str,
        payload: dict[str, Any],
    ) -> None:
        saver = getattr(self._prompt_repository, "save_prompt_record", None)
        if callable(saver):
            saver(prompt_id, dict(payload))
