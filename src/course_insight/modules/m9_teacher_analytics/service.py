"""Formal M9 teacher-analytics service boundary."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from typing import Any

from course_insight.contracts.analytics import (
    ReviewQueueItem,
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMGenerationResult,
    ModelInvocationAudit,
    SafetyCheckResult,
)
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import (
    CalibrationRunResult,
    ModelQualityReport,
)
from course_insight.contracts.platform import ActorContext, TeacherReviewSubmission
from course_insight.contracts.state import StateUpdateResult
from course_insight.infrastructure.deepseek import EmptyDeepSeekAdapter
from course_insight.infrastructure.json_io import read_json
from course_insight.modules.m9_teacher_analytics.adapter import (
    GovernedM9NarrativeAdapter,
    M9InvocationFailure,
    M9NarrativeOutcome,
)
from course_insight.modules.m9_teacher_analytics.repository import (
    M9ModelAuditRecord,
    M9Repository,
)
from course_insight.modules.m9_teacher_analytics.reports import (
    build_class_report,
    build_individual_report,
    latest_audits,
)
from course_insight.modules.m9_teacher_analytics.suggestions import (
    TeacherThresholdPolicy,
    build_teaching_suggestions,
)


def _read_verified_teacher_policy(
    path: Path,
    expected_checksum: str,
) -> TeacherThresholdPolicy:
    try:
        content = path.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise DomainError(
            code="REPORT_SCOPE_INVALID",
            module="m9",
            message="teacher threshold policy could not be loaded",
            details={
                "policy": "teacher_threshold",
                "reason": "unavailable",
            },
            recoverable=True,
        ) from exc
    actual_checksum = hashlib.sha256(content).hexdigest()
    if type(expected_checksum) is not str or not hmac.compare_digest(
        actual_checksum,
        expected_checksum,
    ):
        raise DomainError(
            code="REPORT_SCOPE_INVALID",
            module="m9",
            message="teacher threshold policy does not match the frozen dependency",
            details={
                "policy": "teacher_threshold",
                "reason": "checksum_mismatch",
            },
            recoverable=True,
        )
    return TeacherThresholdPolicy.from_bytes(content)


class M9TeacherAnalyticsService:
    """Build evidence-aware teacher reports and review decisions."""

    def __init__(
        self,
        repository: M9Repository,
        statistics_engine: Any,
        suggestion_rule_engine: Any,
    ) -> None:
        self._repository = repository
        self._statistics_engine = statistics_engine
        self._suggestion_rule_engine = suggestion_rule_engine
        self._narrative_adapter: GovernedM9NarrativeAdapter | None = None

    def configure_teacher_interpreter(
        self,
        adapter: GovernedM9NarrativeAdapter,
    ) -> None:
        """Explicitly enable the optional, default-off DeepSeek path."""

        if not isinstance(adapter, GovernedM9NarrativeAdapter):
            raise TypeError("adapter must implement GovernedM9NarrativeAdapter")
        if self._narrative_adapter is not None:
            raise RuntimeError("M9 teacher interpreter is already configured")
        self._narrative_adapter = adapter

    def generate_teacher_narrative(
        self,
        request: LLMGenerationRequest,
    ) -> LLMGenerationResult:
        """Return the governed DeepSeek teacher-narrative placeholder.

        原始输入：M9 教师叙事用途的 LLMGenerationRequest。
        契约来源：intelligence 中的 DeepSeek 请求与结果契约。
        返回消费者：AppCoordinator 和后续教师分析界面。
        业务校验：不读取密钥、不访问网络且不生成教师叙事。
        错误码：LLM_USE_CASE_INVALID；合法请求固定返回 empty。
        """

        if request.use_case != "teacher_narrative":
            raise DomainError(
                code="LLM_USE_CASE_INVALID",
                module="m9",
                message="M9 accepts only teacher-narrative generation",
            )
        return EmptyDeepSeekAdapter().generate(request)

    def interpret_teacher_analytics(
        self,
        actor_context: ActorContext,
        analytics_bundle: TeacherAnalyticsBundle,
    ) -> LLMGenerationResult:
        """Generate a teacher-only interpretation of authoritative analytics.

        原始输入：教师权限上下文和已经持久化的 TeacherAnalyticsBundle。
        契约来源：M0 ActorContext 及 M3/M8/M5 的确定性分析结果。
        返回消费者：M0 教师分析界面。
        业务校验：仅教师本人班级、仅权威报告、仅匿名班级聚合事实；模型
        不能重算、改建议、读取个体数据或替教师决策。
        错误码：MODEL_ADAPTER_UNCONFIGURED、MODEL_API_UNAVAILABLE、
        MODEL_OUTPUT_BLOCKED、INVALID_MODEL_JSON、REPORT_SCOPE_INVALID、
        TEACHER_INTERPRETATION_FORBIDDEN。
        """

        if (
            actor_context.role != "teacher"
            or analytics_bundle.class_report.class_id
            not in actor_context.class_ids
            or not actor_context.course_ids
        ):
            raise DomainError(
                code="TEACHER_INTERPRETATION_FORBIDDEN",
                module="m9",
                message="teacher interpretation requires an authorized teacher class",
                recoverable=False,
            )
        scoped_getter = getattr(
            self._repository,
            "get_scoped_analytics",
            None,
        )
        authoritative = None
        if callable(scoped_getter):
            for course_id in actor_context.course_ids:
                authoritative = scoped_getter(
                    analytics_bundle.report_id,
                    course_id=course_id,
                    class_id=analytics_bundle.class_report.class_id,
                )
                if authoritative is not None:
                    break
        if authoritative is None:
            raise DomainError(
                code="TEACHER_INTERPRETATION_FORBIDDEN",
                module="m9",
                message=(
                    "teacher interpretation requires an authorized "
                    "course and class report"
                ),
                recoverable=False,
            )
        if (
            not hmac.compare_digest(
                authoritative.content_checksum(),
                analytics_bundle.content_checksum(),
            )
            or authoritative != analytics_bundle
        ):
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="teacher interpretation requires the authoritative M9 report",
                details={"report_id": analytics_bundle.report_id},
                recoverable=True,
            )
        if self._narrative_adapter is None:
            raise DomainError(
                code="MODEL_ADAPTER_UNCONFIGURED",
                module="m9",
                message="the governed teacher-interpretation adapter is not configured",
                recoverable=True,
            )
        try:
            outcome = self._narrative_adapter.narrate(analytics_bundle)
        except M9InvocationFailure as failure:
            self._record_model_call(
                prompt_record=failure.prompt_record,
                generation=failure.generation,
                invocation=failure.audit,
                safety=failure.safety,
            )
            raise failure.error from None
        if not isinstance(outcome, M9NarrativeOutcome):
            raise DomainError(
                code="INVALID_MODEL_JSON",
                module="m9",
                message="the governed narrative adapter returned an invalid outcome",
                details={"report_id": analytics_bundle.report_id},
                recoverable=True,
            )
        self._record_model_call(
            prompt_record=outcome.prompt_record,
            generation=outcome.generation,
            invocation=outcome.audit,
            safety=outcome.safety,
        )
        return outcome.generation.model_copy(deep=True)

    def build_model_quality_report(
        self,
        calibration_result: CalibrationRunResult,
        requested_at: datetime,
    ) -> ModelQualityReport:
        """Return an insufficient-data report for empty IRT calibration.

        原始输入：M8 标定结果和质量检查请求时间。
        契约来源：CalibrationRunResult 与 ModelQualityReport。
        返回消费者：AppCoordinator、教师复核和后续模型发布门。
        业务校验：空标定不得声明质量指标或可发布状态。
        错误码：无；当前空实现固定返回 insufficient_data。
        """

        return ModelQualityReport(
            report_id=f"quality_empty_{calibration_result.run_id}",
            subject_ref=calibration_result.parameter_set.parameter_set_id,
            metrics={},
            observation_count=calibration_result.parameter_set.sample_size,
            status="insufficient_data",
            generated_at=requested_at,
        )

    def build_teacher_analytics(
        self,
        knowledge_bundle: KnowledgeBundle,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        teacher_threshold_policy_path: Path,
    ) -> TeacherAnalyticsBundle:
        """Build class, learner, review, and suggestion analytics.

        原始输入：M3 知识、M8 评分、M5 状态和教师阈值策略。
        契约来源：三个前驱服务输出及本地策略 JSON。
        返回消费者：教师端分析与复核工作流。
        业务校验：范围、覆盖率、人数、置信度、触发指标和证据必须一致。
        错误码：INSUFFICIENT_CLASS_EVIDENCE。
        """

        self._validate_scope(
            knowledge_bundle,
            scoring_result_bundle,
            state_update_result,
        )
        policy = TeacherThresholdPolicy.from_path(
            teacher_threshold_policy_path
        )
        return self._build_teacher_analytics_with_policy(
            knowledge_bundle=knowledge_bundle,
            scoring_result_bundle=scoring_result_bundle,
            state_update_result=state_update_result,
            policy=policy,
        )

    def build_teacher_analytics_with_frozen_policy(
        self,
        knowledge_bundle: KnowledgeBundle,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        teacher_threshold_policy_path: Path,
        expected_policy_checksum: str,
    ) -> TeacherAnalyticsBundle:
        """Build analytics using the exact policy bytes identified by M0."""

        self._validate_scope(
            knowledge_bundle,
            scoring_result_bundle,
            state_update_result,
        )
        policy = _read_verified_teacher_policy(
            teacher_threshold_policy_path,
            expected_policy_checksum,
        )
        return self._build_teacher_analytics_with_policy(
            knowledge_bundle=knowledge_bundle,
            scoring_result_bundle=scoring_result_bundle,
            state_update_result=state_update_result,
            policy=policy,
        )

    def _build_teacher_analytics_with_policy(
        self,
        *,
        knowledge_bundle: KnowledgeBundle,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        policy: TeacherThresholdPolicy,
    ) -> TeacherAnalyticsBundle:
        class_report = build_class_report(
            scoring_result_bundle,
            state_update_result,
        )
        individual = build_individual_report(
            scoring_result_bundle,
            state_update_result,
            weak_mastery_threshold=policy.weak_mastery_threshold,
            misconception_threshold=policy.misconception_threshold,
        )
        queue = [
            ReviewQueueItem(
                audit_id=record.audit_id,
                audit_version=record.audit_version,
                learner_id=scoring_result_bundle.learner_id,
                item_instance_id=record.item_instance_id,
                recommended_score=record.total_score,
                confidence=record.confidence,
                review_reasons=(
                    list(record.review_reason)
                    if record.review_reason
                    else ["review_required"]
                ),
            )
            for record in latest_audits(scoring_result_bundle)
            if record.needs_review()
        ]
        queue.sort(key=lambda item: item.priority_key())
        bundle = TeacherAnalyticsBundle(
            report_id=teacher_analytics_report_id(
                course_id=knowledge_bundle.course_id,
                class_state_snapshot_id=(
                    state_update_result.class_state_snapshot.snapshot_id
                ),
            ),
            class_report=class_report,
            individual_reports=[individual],
            review_queue=queue,
            teaching_suggestions=build_teaching_suggestions(
                state_update_result,
                policy,
            ),
            generated_at=state_update_result.updated_at,
        )
        insert_or_get = getattr(
            self._repository,
            "insert_or_get_analytics",
            None,
        )
        if callable(insert_or_get):
            authoritative = insert_or_get(
                bundle.model_copy(deep=True),
                course_id=knowledge_bundle.course_id,
            )
            if authoritative != bundle:
                raise RuntimeError("M9 persisted analytics conflicts with result")
            return authoritative.model_copy(deep=True)
        saver = getattr(self._repository, "save_analytics", None)
        if callable(saver):
            saver(bundle.model_copy(deep=True))
        return bundle

    def get_analytics(
        self,
        report_id: str,
    ) -> TeacherAnalyticsBundle | None:
        getter = getattr(self._repository, "get_analytics", None)
        if not callable(getter):
            return None
        bundle = getter(report_id)
        return None if bundle is None else bundle.model_copy(deep=True)

    def get_latest_analytics(
        self,
        *,
        course_id: str,
        class_id: str,
        learner_id: str | None = None,
    ) -> TeacherAnalyticsBundle | None:
        getter = getattr(self._repository, "get_latest_analytics", None)
        if not callable(getter):
            return None
        bundle = getter(
            course_id=course_id,
            class_id=class_id,
            learner_id=learner_id,
        )
        return None if bundle is None else bundle.model_copy(deep=True)

    def record_teacher_review(
        self,
        raw_review_path: Path | TeacherReviewSubmission,
        current_scoring_result_bundle: ScoringResultBundle,
    ) -> TeacherReviewDecision:
        """Validate a raw teacher form into a version-bound decision.

        原始输入：M0 教师提交契约或复核 JSON 路径，以及当前 M8 评分包。
        契约来源：M0/教师原始表单与 finalize_scoring 当前输出。
        返回消费者：M8.apply_teacher_review。
        业务校验：审计身份、期望版本、决定类型和覆盖分数必须一致。
        错误码：REPORT_SCOPE_INVALID。
        """

        try:
            if isinstance(raw_review_path, TeacherReviewSubmission):
                decision = TeacherReviewDecision(
                    decision_id=raw_review_path.submission_id,
                    audit_id=raw_review_path.audit_id,
                    expected_audit_version=(
                        raw_review_path.expected_audit_version
                    ),
                    decision=raw_review_path.decision,
                    final_total_score=raw_review_path.final_total_score,
                    criterion_overrides=[
                        item.model_copy(deep=True)
                        for item in raw_review_path.criterion_overrides
                    ],
                    teacher_comment=raw_review_path.teacher_comment,
                    reviewer_id=raw_review_path.reviewer_id,
                    reviewed_at=raw_review_path.submitted_at,
                )
            else:
                payload = read_json(raw_review_path)
                if type(payload) is not dict:
                    raise DomainError(
                        code="REPORT_SCOPE_INVALID",
                        module="m9",
                        message="teacher review payload must be a JSON object",
                    )
                decision = TeacherReviewDecision.model_validate(payload)
        except Exception as error:
            details = (
                {"file_name": raw_review_path.name}
                if isinstance(raw_review_path, Path)
                else {"submission_id": raw_review_path.submission_id}
            )
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="teacher review input is invalid",
                details=details,
                recoverable=True,
            ) from error
        if decision.audit_id not in {
            record.audit_id
            for record in current_scoring_result_bundle.score_audit_records
        }:
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="teacher review references an unknown score audit",
                details={"audit_id": decision.audit_id},
                recoverable=True,
            )
        current = current_scoring_result_bundle.get_audit_record(
            decision.audit_id
        )
        decision.assert_matches(current)
        insert_or_get = getattr(
            self._repository,
            "insert_or_get_review_decision",
            None,
        )
        if callable(insert_or_get):
            authoritative = insert_or_get(decision.model_copy(deep=True))
            if authoritative != decision:
                raise RuntimeError(
                    "M9 persisted teacher review conflicts with result"
                )
            return authoritative.model_copy(deep=True)
        saver = getattr(self._repository, "save_review_decision", None)
        if callable(saver):
            saver(decision.model_copy(deep=True))
        return decision

    def get_review_decision(
        self,
        decision_id: str,
    ) -> TeacherReviewDecision | None:
        getter = getattr(self._repository, "get_review_decision", None)
        if not callable(getter):
            return None
        decision = getter(decision_id)
        return None if decision is None else decision.model_copy(deep=True)

    def _record_model_call(
        self,
        *,
        prompt_record: dict[str, Any],
        generation: LLMGenerationResult,
        invocation: ModelInvocationAudit,
        safety: SafetyCheckResult,
    ) -> None:
        record = M9ModelAuditRecord.from_artifacts(
            prompt_record=dict(prompt_record),
            generation=generation.model_copy(deep=True),
            invocation=invocation.model_copy(deep=True),
            safety=safety.model_copy(deep=True),
        )
        saver = getattr(self._repository, "save_model_audit", None)
        if not callable(saver):
            raise RuntimeError("M9 repository does not persist model-call audits")
        saver(record)

    @staticmethod
    def _validate_scope(
        knowledge_bundle: KnowledgeBundle,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
    ) -> None:
        learner = state_update_result.learner_state_snapshot
        class_state = state_update_result.class_state_snapshot
        concept_ids = {concept.concept_id for concept in knowledge_bundle.concepts}
        state_concept_ids = {
            concept.concept_id for concept in learner.concept_states
        }
        if (
            knowledge_bundle.course_id != learner.course_id
            or learner.course_id != class_state.course_id
            or scoring_result_bundle.learner_id != learner.learner_id
            or scoring_result_bundle.attempt_id
            != state_update_result.diagnosis_result.attempt_id
            or not state_concept_ids <= concept_ids
        ):
            raise DomainError(
                code="REPORT_SCOPE_INVALID",
                module="m9",
                message="knowledge, score, and state report scopes must align",
                recoverable=True,
            )


def teacher_analytics_report_id(
    *,
    course_id: str,
    class_state_snapshot_id: str,
) -> str:
    return f"report_{course_id}_{class_state_snapshot_id}"
