"""Formal M7 local-model service boundary."""

from __future__ import annotations

from typing import Any

from course_insight.contracts.assessment import (
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceBundle
from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMGenerationResult,
)
from course_insight.contracts.tutoring import (
    EvidenceCitation,
    FeedbackGenerationTask,
    StudentFeedbackPackage,
)
from course_insight.infrastructure.deepseek import EmptyDeepSeekAdapter
from course_insight.modules.m7_local_model.prompts import (
    FEEDBACK_PROMPT_ID,
    SCORING_PROMPT_ID,
    feedback_message,
)
from course_insight.modules.m7_local_model.repository import M7Repository


class M7LocalModelService:
    """Validate course-grounded local scoring and feedback outputs."""

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
        错误码：INVALID_MODEL_JSON。
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
        scorer = getattr(self._local_model_adapter, "score", None)
        if not callable(scorer):
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="the configured rubric adapter cannot score answers",
                details={"scoring_task_id": rubric_scoring_task.scoring_task_id},
                recoverable=True,
            )
        result = scorer(rubric_scoring_task, evidence_bundle)
        self._validate_scoring_result(
            rubric_scoring_task,
            evidence_bundle,
            result,
        )
        saver = getattr(self._prompt_repository, "save_prompt_record", None)
        if callable(saver):
            saver(
                SCORING_PROMPT_ID,
                {
                    "scoring_task_id": rubric_scoring_task.scoring_task_id,
                    "evidence_query_id": evidence_bundle.query_id,
                    "adapter": result.model_name,
                },
            )
        audit_saver = getattr(self._prompt_repository, "save_model_audit", None)
        if callable(audit_saver):
            audit_saver(
                f"model_audit_{rubric_scoring_task.scoring_task_id}",
                result.model_copy(deep=True),
            )
        return result

    def generate_student_feedback(
        self,
        feedback_generation_task: FeedbackGenerationTask,
        evidence_bundle: EvidenceBundle,
    ) -> StudentFeedbackPackage:
        """Generate cited learner-safe feedback.

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
                quote=chunk.text,
            )
            for chunk in evidence_bundle.evidence_chunks
        ]
        target_ids = feedback_generation_task.target_concept_ids()
        package = StudentFeedbackPackage(
            feedback_id=(
                f"feedback_{feedback_generation_task.feedback_task_id}"
            ),
            task_id=feedback_generation_task.task_id,
            learner_id=feedback_generation_task.learner_id,
            message=feedback_message(target_ids),
            rubric_feedback=[],
            missing_concept_ids=target_ids,
            evidence_citations=citations,
            next_practice_item_ids=[
                f"practice_{concept_id}" for concept_id in target_ids
            ],
            confidence=0.6,
            generated_at=feedback_generation_task.created_at,
        )
        if not package.safe_for_student():
            raise DomainError(
                code="EVIDENCE_REQUIRED",
                module="m7",
                message="student feedback must remain cited and answer-safe",
                details={
                    "feedback_task_id": feedback_generation_task.feedback_task_id
                },
                recoverable=True,
            )
        saver = getattr(self._prompt_repository, "save_prompt_record", None)
        if callable(saver):
            saver(
                FEEDBACK_PROMPT_ID,
                {
                    "feedback_task_id": feedback_generation_task.feedback_task_id,
                    "evidence_query_id": evidence_bundle.query_id,
                    "must_hide_answer": feedback_generation_task.must_hide_answer(),
                },
            )
        feedback_saver = getattr(self._prompt_repository, "save_feedback", None)
        if callable(feedback_saver):
            feedback_saver(package.model_copy(deep=True))
        return package

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
    ) -> None:
        if not isinstance(result, RubricScoringResult):
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m7",
                message="the rubric adapter returned an invalid result type",
                details={"scoring_task_id": task.scoring_task_id},
                recoverable=True,
            )
        available_ids = set(evidence_bundle.citation_ids())
        for score in result.criterion_scores:
            criterion = task.rubric.criterion(score.criterion_id)
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
        validator = getattr(self._output_validator, "validate", None)
        if callable(validator):
            accepted = validator(result)
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
