"""Formal M5 learner-and-class-state service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import (
    CognitiveDiagnosisResult,
    KnowledgeTraceSnapshot,
    LearningModelRun,
    LearningObservationBatch,
)
from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.modules.m5_learner_class_state.repository import M5Repository
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
    StatePolicy,
    audit_version_key,
    latest_audits,
)


class M5StateService:
    """Diagnose scoring evidence into versioned learner and class state."""

    def __init__(
        self,
        repository: M5Repository,
        state_update_policy: Any,
        class_aggregation_policy: Any,
    ) -> None:
        self._repository = repository
        self._state_update_policy = state_update_policy
        self._class_aggregation_policy = class_aggregation_policy
        self._processed_by_learner: dict[str, frozenset[str]] = {}

    def update_state(
        self,
        scoring_result_bundle: ScoringResultBundle,
        knowledge_bundle: KnowledgeBundle,
        previous_learner_state_snapshot: LearnerStateSnapshot | None,
        previous_class_state_snapshot: ClassStateSnapshot | None,
        state_policy_path: Path,
    ) -> StateUpdateResult:
        """Update diagnosis plus learner and class state atomically.

        原始输入：M8 评分包、M3 知识包、可选旧快照和状态策略。
        契约来源：M8/M3 输出及 M5 明示自历史。
        返回消费者：M6 教学控制、M8 巩固和 M9 分析。
        业务校验：版本、证据审计、身份、覆盖率和聚合阈值必须一致。
        错误码：INSUFFICIENT_EVIDENCE。
        """

        policy = StatePolicy.from_path(state_policy_path)
        audits = latest_audits(scoring_result_bundle)
        audit_keys = frozenset(
            audit_version_key(record)
            for record in scoring_result_bundle.score_audit_records
        )
        if not audits or not audit_keys:
            raise DomainError(
                code="INSUFFICIENT_EVIDENCE",
                module="m5",
                message="state updating requires score audit evidence",
                details={"attempt_id": scoring_result_bundle.attempt_id},
                recoverable=True,
            )
        seen = self._processed_by_learner.get(
            scoring_result_bundle.learner_id,
            frozenset(),
        )
        latest_version = max(record.audit_version for record in audits)
        if audit_keys <= seen or (
            previous_learner_state_snapshot is not None
            and latest_version <= previous_learner_state_snapshot.state_version
        ):
            raise DomainError(
                code="STALE_STATE_VERSION",
                module="m5",
                message="the exact versioned score evidence was already processed",
                details={"audit_keys": sorted(audit_keys)},
                recoverable=True,
            )
        update_policy = (
            self._state_update_policy
            if isinstance(self._state_update_policy, DeterministicStateUpdatePolicy)
            else DeterministicStateUpdatePolicy()
        )
        aggregation_policy = (
            self._class_aggregation_policy
            if isinstance(
                self._class_aggregation_policy,
                DeterministicClassAggregationPolicy,
            )
            else DeterministicClassAggregationPolicy()
        )
        diagnosis = update_policy.build_diagnosis(
            scoring_result_bundle,
            knowledge_bundle,
        )
        learner = update_policy.build_learner_state(
            scoring_result_bundle,
            knowledge_bundle,
            diagnosis,
            previous_learner_state_snapshot,
            policy,
        )
        class_state = aggregation_policy.aggregate(
            learner,
            previous_class_state_snapshot,
            policy,
        )
        result = StateUpdateResult(
            diagnosis_result=diagnosis,
            learner_state_snapshot=learner,
            class_state_snapshot=class_state,
            processed_audit_ids=sorted(audit_keys),
            updated_at=scoring_result_bundle.finalized_at,
        )
        self._processed_by_learner = {
            **self._processed_by_learner,
            scoring_result_bundle.learner_id: seen | audit_keys,
        }
        return result

    def run_learning_models(
        self,
        observation_batch: LearningObservationBatch,
    ) -> LearningModelRun:
        """Return empty DINA and BKT outputs for the governed learner batch.

        原始输入：M8 评分审计转换得到的 LearningObservationBatch。
        契约来源：learning_models 中的批次、DINA、BKT 与运行契约。
        返回消费者：AppCoordinator、M6 诊断编排和后续模型实现。
        业务校验：保留学习者、水位和观测计数，不执行估计或伪造概率。
        错误码：无；当前空实现固定返回 empty。
        """

        observation_count = len(observation_batch.observations)
        diagnosis = CognitiveDiagnosisResult(
            run_id=f"dina_empty_{observation_batch.batch_id}",
            learner_id=observation_batch.learner_id,
            model_type="DINA",
            model_version="unconfigured",
            concept_mastery={},
            observation_count=observation_count,
            status="empty",
            generated_at=observation_batch.created_at,
        )
        knowledge_trace = KnowledgeTraceSnapshot(
            trace_id=f"bkt_empty_{observation_batch.batch_id}",
            learner_id=observation_batch.learner_id,
            model_type="BKT",
            model_version="unconfigured",
            concept_probabilities={},
            observation_watermark=observation_batch.watermark,
            observation_count=observation_count,
            status="empty",
            updated_at=observation_batch.created_at,
        )
        return LearningModelRun(
            run_id=f"learning_models_empty_{observation_batch.batch_id}",
            diagnosis=diagnosis,
            knowledge_trace=knowledge_trace,
            observation_count=observation_count,
            status="empty",
            created_at=observation_batch.created_at,
        )
