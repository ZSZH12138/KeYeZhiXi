"""Formal M5 learner-and-class-state service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from course_insight.contracts.assessment import ScoreAuditRecord, ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.learning_models import (
    CognitiveDiagnosisResult,
    KnowledgeTraceSnapshot,
    LearningModelRun,
    LearningObservation,
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
        # M5-07: load processed audit watermark from repository for restart recovery
        seen = self._processed_by_learner.get(
            scoring_result_bundle.learner_id,
        )
        if seen is None:
            seen = self._repository.get_processed_audits(
                scoring_result_bundle.learner_id
            )
            self._processed_by_learner = {
                **self._processed_by_learner,
                scoring_result_bundle.learner_id: seen,
            }
        # M5-01: filter out already-processed audits from mixed batches
        new_audit_keys = audit_keys - seen
        if not new_audit_keys:
            raise DomainError(
                code="STALE_STATE_VERSION",
                module="m5",
                message="the exact versioned score evidence was already processed",
                details={"audit_keys": sorted(audit_keys)},
                recoverable=True,
            )
        # Only process audits not yet seen; old audits in a mixed batch are skipped
        audits = [
            record
            for record in audits
            if audit_version_key(record) in new_audit_keys
        ]
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
        # M5-07: recover previous state from repository when not provided by caller
        if previous_learner_state_snapshot is None:
            previous_learner_state_snapshot = (
                self._repository.get_latest_learner_state(
                    knowledge_bundle.course_id,
                    policy.class_id,
                    scoring_result_bundle.learner_id,
                )
            )
        if previous_class_state_snapshot is None:
            previous_class_state_snapshot = (
                self._repository.get_latest_class_state(
                    knowledge_bundle.course_id,
                    policy.class_id,
                )
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
            processed_audit_ids=sorted(new_audit_keys),
            updated_at=scoring_result_bundle.finalized_at,
        )
        # M5-07: persist state directly through the repository (no getattr)
        self._repository.save_learner_state(learner.model_copy(deep=True))
        self._repository.save_class_state(class_state.model_copy(deep=True))
        self._repository.save_processed_audits(
            scoring_result_bundle.learner_id,
            seen | new_audit_keys,
        )
        self._processed_by_learner = {
            **self._processed_by_learner,
            scoring_result_bundle.learner_id: seen | new_audit_keys,
        }
        return result

    @staticmethod
    def convert_to_observations(
        bundle: ScoringResultBundle,
        knowledge: KnowledgeBundle,
    ) -> LearningObservationBatch:
        """Convert M8 score audit records into a learning observation batch.

        M5-06: Bridge the M8 scoring output to the M5 learning-model input.
        Each latest-version ScoreAuditRecord becomes one LearningObservation,
        with concept_ids resolved from the Q-matrix.
        """

        # Build Q-matrix lookup for per-item concept resolution
        q_lookup: dict[tuple[str, str], list[str]] = {}
        for entry in knowledge.q_matrix:
            if entry.is_active():
                q_lookup.setdefault(
                    (entry.item_id, entry.item_version), []
                ).append(entry.concept_id)

        # Build reverse lookup: item_instance_id → (item_id, item_version)
        # by replicating the paper generator's instance ID derivation.
        item_identity: dict[str, tuple[str, str]] = {}
        for item in knowledge.approved_items():
            prefix, separator, suffix = item.item_id.rpartition("_")
            instance_id = (
                f"{prefix}_instance_{suffix}"
                if separator and suffix.isdigit()
                else f"{item.item_id}_instance"
            )
            item_identity[instance_id] = (item.item_id, item.version)

        # Recover course_id/class_id from learning events (contract requires non-empty)
        course_id = ""
        class_id = ""
        for event in bundle.learning_events:
            course_id = event.course_id
            class_id = event.class_id
            break
        if not course_id or not class_id:
            raise DomainError(
                code="STATE_SCOPE_MISMATCH",
                module="m5",
                message="cannot recover course/class identity without learning events",
                details={"attempt_id": bundle.attempt_id},
            )

        observations: list[LearningObservation] = []
        # Use latest audit per audit_id (same logic as latest_audits)
        latest: dict[str, ScoreAuditRecord] = {}
        for record in bundle.score_audit_records:
            current = latest.get(record.audit_id)
            if current is None or record.audit_version > current.audit_version:
                latest[record.audit_id] = record
        for audit_id in sorted(latest):
            audit = latest[audit_id]
            identity = item_identity.get(audit.item_instance_id)
            if identity is None:
                continue
            item_id, item_version = identity
            concepts = q_lookup.get(
                (item_id, item_version),
                [],
            )
            if not concepts:
                continue
            observations.append(
                LearningObservation(
                    observation_id=(
                        f"obs_{audit.audit_id}_v{audit.audit_version}"
                    ),
                    learner_id=bundle.learner_id,
                    course_id=course_id,
                    class_id=class_id,
                    attempt_id=bundle.attempt_id,
                    item_id=item_id,
                    item_version=item_version,
                    concept_ids=concepts,
                    score=audit.total_score,
                    max_score=audit.max_score,
                    source_audit_id=audit.audit_id,
                    source_audit_version=audit.audit_version,
                    occurred_at=audit.created_at,
                )
            )
        watermark = (
            f"wm_{bundle.attempt_id}_"
            f"{bundle.finalized_at.isoformat()}"
        )
        return LearningObservationBatch(
            batch_id=f"batch_{bundle.attempt_id}",
            learner_id=bundle.learner_id,
            observations=observations,
            watermark=watermark,
            created_at=bundle.finalized_at,
        )

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

        D-02 修复：真正的 DINA 合取诊断引擎尚未实现（U-01），
        此前用知识点得分率平均值冒充 DINA estimated 属于名实不符。
        遵守「证据不足不伪造」边界——继续返回 empty。
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
