"""Formal M5 learner-and-class-state service boundary."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle, QMatrixEntry
from course_insight.contracts.learning_models import (
    CognitiveDiagnosisResult,
    DinaModelArtifact,
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
from course_insight.modules.m5_learner_class_state.dina import DinaEngine
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
    StatePolicy,
    audit_version_key,
    latest_audits,
)


def _read_verified_state_policy(
    path: Path,
    expected_checksum: str,
) -> StatePolicy:
    try:
        content = path.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise DomainError(
            code="STATE_POLICY_INVALID",
            module="m5",
            message="state policy could not be loaded",
            details={"policy": "state", "reason": "unavailable"},
            recoverable=True,
        ) from exc
    actual_checksum = hashlib.sha256(content).hexdigest()
    if type(expected_checksum) is not str or not hmac.compare_digest(
        actual_checksum,
        expected_checksum,
    ):
        raise DomainError(
            code="STATE_POLICY_INVALID",
            module="m5",
            message="state policy does not match the frozen dependency",
            details={"policy": "state", "reason": "checksum_mismatch"},
            recoverable=True,
        )
    return StatePolicy.from_bytes(content)


class M5StateService:
    """Diagnose scoring evidence into versioned learner and class state."""

    def __init__(
        self,
        repository: M5Repository,
        state_update_policy: Any,
        class_aggregation_policy: Any,
        *,
        dina_engine: DinaEngine | None = None,
    ) -> None:
        self._repository = repository
        self._state_update_policy = state_update_policy
        self._class_aggregation_policy = class_aggregation_policy
        self._dina_engine = dina_engine or DinaEngine()
        self._processed_by_scope: dict[
            tuple[str, str, str],
            frozenset[str],
        ] = {}

    def fit_dina_model(
        self,
        cohort: list[LearningObservationBatch],
        q_matrix: list[QMatrixEntry],
    ) -> DinaModelArtifact:
        """Persist governed observations and fit one append-only DINA model."""

        observation_writer = getattr(
            self._repository,
            "insert_or_get_learning_observation_batch",
            None,
        )
        if callable(observation_writer):
            for batch in cohort:
                authoritative = observation_writer(batch.model_copy(deep=True))
                if authoritative != batch:
                    raise RuntimeError(
                        "M5 persisted learning observations conflict with input"
                    )
        model = self._dina_engine.fit(cohort, q_matrix)
        model_writer = getattr(self._repository, "insert_or_get_dina_model", None)
        if callable(model_writer):
            authoritative_model = model_writer(model.model_copy(deep=True))
            if authoritative_model != model:
                raise RuntimeError("M5 persisted DINA model conflicts with result")
            model = authoritative_model
        return model.model_copy(deep=True)

    def infer_dina(
        self,
        model: DinaModelArtifact,
        batch: LearningObservationBatch,
    ) -> CognitiveDiagnosisResult:
        """Apply one exact DINA model version to a learner batch."""

        return self._dina_engine.infer(
            model.model_copy(deep=True),
            batch.model_copy(deep=True),
        )

    def get_state_update(
        self,
        attempt_id: str,
    ) -> StateUpdateResult | None:
        """Recover one complete attempt-bound state update."""

        getter = getattr(self._repository, "get_state_update", None)
        if not callable(getter):
            getter = getattr(
                self._repository,
                "get_state_update_result",
                None,
            )
        if not callable(getter):
            return None
        result = getter(attempt_id)
        return None if result is None else result.model_copy(deep=True)

    def get_state_update_result(
        self,
        attempt_id: str,
    ) -> StateUpdateResult | None:
        return self.get_state_update(attempt_id)

    def get_state_update_version(
        self,
        attempt_id: str,
        state_version: int,
    ) -> StateUpdateResult | None:
        result = self._repository.get_state_update_version(
            attempt_id,
            state_version,
        )
        return None if result is None else result.model_copy(deep=True)

    def get_state_update_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> StateUpdateResult | None:
        result = self._repository.get_state_update_for_audit(
            attempt_id,
            audit_id,
            audit_version,
        )
        return None if result is None else result.model_copy(deep=True)

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Recover one exact learner baseline without a latest-state lookup."""

        getter = getattr(self._repository, "get_learner_state_exact", None)
        if not callable(getter):
            return None
        snapshot = getter(
            course_id,
            class_id,
            learner_id,
            state_version,
        )
        if snapshot is None:
            return None
        if not isinstance(snapshot, LearnerStateSnapshot) or (
            snapshot.course_id,
            snapshot.class_id,
            snapshot.learner_id,
            snapshot.state_version,
        ) != (course_id, class_id, learner_id, state_version):
            raise RuntimeError(
                "M5 exact learner-state scope mismatch"
            )
        return snapshot.model_copy(deep=True)

    def get_class_state_exact(
        self,
        course_id: str,
        class_id: str,
        state_version: int,
    ) -> ClassStateSnapshot | None:
        """Recover one exact class baseline without a latest-state lookup."""

        getter = getattr(self._repository, "get_class_state_exact", None)
        if not callable(getter):
            return None
        snapshot = getter(course_id, class_id, state_version)
        if snapshot is None:
            return None
        if not isinstance(snapshot, ClassStateSnapshot) or (
            snapshot.course_id,
            snapshot.class_id,
        ) != (course_id, class_id):
            raise RuntimeError(
                "M5 exact class-state scope mismatch"
            )
        return snapshot.model_copy(deep=True)

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ) -> ClassStateSnapshot | None:
        """Recover one exact class baseline by its scoped identity."""

        getter = getattr(
            self._repository,
            "get_class_state_by_identity",
            None,
        )
        if not callable(getter):
            return None
        snapshot = getter(course_id, class_id, snapshot_id)
        if snapshot is None:
            return None
        if not isinstance(snapshot, ClassStateSnapshot) or (
            snapshot.course_id,
            snapshot.class_id,
            snapshot.snapshot_id,
        ) != (course_id, class_id, snapshot_id):
            raise RuntimeError(
                "M5 exact class-state identity mismatch"
            )
        return snapshot.model_copy(deep=True)

    def get_latest_learner_state(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        getter = getattr(self._repository, "get_latest_learner_state", None)
        if not callable(getter):
            return None
        snapshot = getter(course_id, class_id, learner_id)
        return None if snapshot is None else snapshot.model_copy(deep=True)

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        getter = getattr(self._repository, "get_latest_class_state", None)
        if not callable(getter):
            return None
        snapshot = getter(course_id, class_id)
        return None if snapshot is None else snapshot.model_copy(deep=True)

    def update_state(
        self,
        scoring_result_bundle: ScoringResultBundle,
        knowledge_bundle: KnowledgeBundle,
        previous_learner_state_snapshot: LearnerStateSnapshot | None,
        previous_class_state_snapshot: ClassStateSnapshot | None,
        state_policy_path: Path,
        learning_observation_batch: LearningObservationBatch | None = None,
    ) -> StateUpdateResult:
        """Update diagnosis plus learner and class state atomically.

        原始输入：M8 评分包、M3 知识包、可选旧快照和状态策略。
        契约来源：M8/M3 输出及 M5 明示自历史。
        返回消费者：M6 教学控制、M8 巩固和 M9 分析。
        业务校验：版本、证据审计、身份、覆盖率和聚合阈值必须一致。
        错误码：INSUFFICIENT_EVIDENCE。
        """

        return self._update_state_with_policy(
            scoring_result_bundle=scoring_result_bundle,
            knowledge_bundle=knowledge_bundle,
            previous_learner_state_snapshot=previous_learner_state_snapshot,
            previous_class_state_snapshot=previous_class_state_snapshot,
            policy=StatePolicy.from_path(state_policy_path),
            learning_observation_batch=learning_observation_batch,
        )

    def update_state_with_frozen_policy(
        self,
        scoring_result_bundle: ScoringResultBundle,
        knowledge_bundle: KnowledgeBundle,
        previous_learner_state_snapshot: LearnerStateSnapshot | None,
        previous_class_state_snapshot: ClassStateSnapshot | None,
        state_policy_path: Path,
        expected_policy_checksum: str,
        learning_observation_batch: LearningObservationBatch | None = None,
    ) -> StateUpdateResult:
        """Update state using the exact policy bytes identified by M0."""

        return self._update_state_with_policy(
            scoring_result_bundle=scoring_result_bundle,
            knowledge_bundle=knowledge_bundle,
            previous_learner_state_snapshot=previous_learner_state_snapshot,
            previous_class_state_snapshot=previous_class_state_snapshot,
            policy=_read_verified_state_policy(
                state_policy_path,
                expected_policy_checksum,
            ),
            learning_observation_batch=learning_observation_batch,
        )

    def _update_state_with_policy(
        self,
        *,
        scoring_result_bundle: ScoringResultBundle,
        knowledge_bundle: KnowledgeBundle,
        previous_learner_state_snapshot: LearnerStateSnapshot | None,
        previous_class_state_snapshot: ClassStateSnapshot | None,
        policy: StatePolicy,
        learning_observation_batch: LearningObservationBatch | None,
    ) -> StateUpdateResult:
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
        scope_key = self._state_scope(
            scoring_result_bundle,
            knowledge_bundle,
            previous_learner_state_snapshot,
            previous_class_state_snapshot,
            authoritative_class_id=policy.class_id,
        )
        seen = self._processed_by_scope.get(scope_key, frozenset())
        watermark_getter = getattr(
            self._repository,
            "get_processed_audit_ids",
            None,
        )
        if callable(watermark_getter):
            seen = seen | watermark_getter(*scope_key)
        if audit_keys <= seen:
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
            learning_observation_batch,
        )
        learner = update_policy.build_learner_state(
            scoring_result_bundle,
            knowledge_bundle,
            diagnosis,
            previous_learner_state_snapshot,
            policy,
        )
        state_lister = getattr(
            self._repository,
            "list_latest_learner_states",
            None,
        )
        persisted_states = (
            state_lister(knowledge_bundle.course_id, policy.class_id)
            if callable(state_lister)
            else []
        )
        states_by_learner = {
            state.learner_id: state.model_copy(deep=True)
            for state in persisted_states
        }
        states_by_learner = {
            **states_by_learner,
            learner.learner_id: learner,
        }
        version_getter = getattr(
            self._repository,
            "get_latest_class_state_version",
            None,
        )
        latest_class_version = (
            version_getter(knowledge_bundle.course_id, policy.class_id)
            if callable(version_getter)
            else None
        )
        class_version = (
            latest_class_version + 1
            if latest_class_version is not None
            else learner.state_version
        )
        class_state = aggregation_policy.aggregate_all(
            list(states_by_learner.values()),
            policy,
            class_version=class_version,
            previous=previous_class_state_snapshot,
        )
        result = StateUpdateResult(
            diagnosis_result=diagnosis,
            learner_state_snapshot=learner,
            class_state_snapshot=class_state,
            processed_audit_ids=sorted(audit_keys),
            updated_at=scoring_result_bundle.finalized_at,
        )
        insert_or_get = getattr(
            self._repository,
            "insert_or_get_state_update",
            None,
        )
        if callable(insert_or_get):
            authoritative = insert_or_get(
                result.model_copy(deep=True),
                expected_previous_class_snapshot_id=(
                    None
                    if previous_class_state_snapshot is None
                    else previous_class_state_snapshot.snapshot_id
                ),
            )
            if authoritative != result:
                raise RuntimeError("M5 persisted state update conflicts with result")
            result = authoritative.model_copy(deep=True)
        self._processed_by_scope = {
            **self._processed_by_scope,
            scope_key: seen | audit_keys,
        }
        return result

    @staticmethod
    def _state_scope(
        scoring_result_bundle: ScoringResultBundle,
        knowledge_bundle: KnowledgeBundle,
        previous_learner_state_snapshot: LearnerStateSnapshot | None,
        previous_class_state_snapshot: ClassStateSnapshot | None,
        *,
        authoritative_class_id: str,
    ) -> tuple[str, str, str]:
        learner_id = scoring_result_bundle.learner_id
        learner_mismatch = (
            previous_learner_state_snapshot is not None
            and (
                previous_learner_state_snapshot.course_id
                != knowledge_bundle.course_id
                or previous_learner_state_snapshot.class_id
                != authoritative_class_id
                or previous_learner_state_snapshot.learner_id != learner_id
            )
        )
        class_mismatch = (
            previous_class_state_snapshot is not None
            and (
                previous_class_state_snapshot.course_id
                != knowledge_bundle.course_id
                or previous_class_state_snapshot.class_id
                != authoritative_class_id
            )
        )
        event_mismatch = any(
            event.course_id != knowledge_bundle.course_id
            or event.class_id != authoritative_class_id
            for event in scoring_result_bundle.learning_events
        )
        if learner_mismatch or class_mismatch or event_mismatch:
            raise DomainError(
                code="STATE_SCOPE_MISMATCH",
                module="m5",
                message="state inputs must match the authoritative policy scope",
                details={"attempt_id": scoring_result_bundle.attempt_id},
                recoverable=True,
            )
        return (
            knowledge_bundle.course_id,
            authoritative_class_id,
            learner_id,
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
