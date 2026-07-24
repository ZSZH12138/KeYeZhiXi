"""M5 run_learning_models DINA/BKT 算法框架验证测试。"""

from datetime import datetime, timedelta, timezone

from course_insight.contracts.learning_models import (
    LearningModelRun,
    LearningObservation,
    LearningObservationBatch,
)


FIXED_DT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


def _make_batch(
    observations: list[LearningObservation],
    batch_id: str = "batch_test",
    learner_id: str = "learner_1",
) -> LearningObservationBatch:
    """构造观测批次的辅助函数。"""
    return LearningObservationBatch(
        batch_id=batch_id,
        learner_id=learner_id,
        observations=observations,
        watermark=f"wm_{batch_id}",
        created_at=FIXED_DT,
    )


class TestM5LearningModelsEmpty:
    """无观测时应返回 empty 状态。"""

    def test_empty_batch_returns_empty(self, m5_service):
        batch = _make_batch([])
        result = m5_service.run_learning_models(batch)
        assert isinstance(result, LearningModelRun)
        assert result.status == "empty"
        assert result.diagnosis.status == "empty"
        assert result.knowledge_trace.status == "empty"
        assert result.diagnosis.concept_mastery == {}
        assert result.knowledge_trace.concept_probabilities == {}

    def test_empty_preserves_learner(self, m5_service):
        batch = _make_batch([])
        result = m5_service.run_learning_models(batch)
        assert result.diagnosis.learner_id == "learner_1"
        assert result.knowledge_trace.learner_id == "learner_1"

    def test_empty_preserves_count(self, m5_service):
        batch = _make_batch([])
        result = m5_service.run_learning_models(batch)
        assert result.observation_count == 0
        assert result.diagnosis.observation_count == 0
        assert result.knowledge_trace.observation_count == 0


class TestM5LearningModelsEstimated:
    """有观测时应返回 estimated/completed 状态。"""

    def test_estimated_status(self, m5_service, observation_batch):
        """有观测时返回 status='completed'。"""
        result = m5_service.run_learning_models(observation_batch)
        assert isinstance(result, LearningModelRun)
        assert result.status == "completed"
        assert result.diagnosis.status == "estimated"
        assert result.knowledge_trace.status == "estimated"

    def test_preserves_learner(self, m5_service, observation_batch):
        """返回结果的 learner_id 应与输入一致。"""
        result = m5_service.run_learning_models(observation_batch)
        assert result.diagnosis.learner_id == observation_batch.learner_id
        assert result.knowledge_trace.learner_id == observation_batch.learner_id

    def test_preserves_count(self, m5_service, observation_batch):
        """observation_count 应等于输入的观测数。"""
        result = m5_service.run_learning_models(observation_batch)
        expected = len(observation_batch.observations)
        assert result.observation_count == expected
        assert result.diagnosis.observation_count == expected
        assert result.knowledge_trace.observation_count == expected

    def test_dina_mastery_full_score(self, m5_service):
        """满分观测 → DINA 掌握概率为 1.0。"""
        batch = _make_batch([
            LearningObservation(
                observation_id="obs_1",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_1",
                item_version="1.0.0",
                concept_ids=["concept_1"],
                score=5.0,
                max_score=5.0,
                source_audit_id="audit_1",
                source_audit_version=1,
                occurred_at=FIXED_DT,
            ),
        ])
        result = m5_service.run_learning_models(batch)
        assert result.diagnosis.concept_mastery["concept_1"] == 1.0

    def test_dina_mastery_zero_score(self, m5_service):
        """零分观测 → DINA 掌握概率为 0.0。"""
        batch = _make_batch([
            LearningObservation(
                observation_id="obs_1",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_1",
                item_version="1.0.0",
                concept_ids=["concept_1"],
                score=0.0,
                max_score=5.0,
                source_audit_id="audit_1",
                source_audit_version=1,
                occurred_at=FIXED_DT,
            ),
        ])
        result = m5_service.run_learning_models(batch)
        assert result.diagnosis.concept_mastery["concept_1"] == 0.0

    def test_dina_mastery_partial_score(self, m5_service):
        """部分得分 → DINA 掌握概率为得分率。"""
        batch = _make_batch([
            LearningObservation(
                observation_id="obs_1",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_1",
                item_version="1.0.0",
                concept_ids=["concept_1"],
                score=3.0,
                max_score=5.0,
                source_audit_id="audit_1",
                source_audit_version=1,
                occurred_at=FIXED_DT,
            ),
        ])
        result = m5_service.run_learning_models(batch)
        assert abs(result.diagnosis.concept_mastery["concept_1"] - 0.6) < 1e-9

    def test_dina_multi_concept(self, m5_service):
        """多概念观测 → 各概念独立计算掌握概率。"""
        batch = _make_batch([
            LearningObservation(
                observation_id="obs_1",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_1",
                item_version="1.0.0",
                concept_ids=["concept_1", "concept_2"],
                score=4.0,
                max_score=5.0,
                source_audit_id="audit_1",
                source_audit_version=1,
                occurred_at=FIXED_DT,
            ),
            LearningObservation(
                observation_id="obs_2",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_2",
                item_version="1.0.0",
                concept_ids=["concept_2"],
                score=5.0,
                max_score=5.0,
                source_audit_id="audit_2",
                source_audit_version=1,
                occurred_at=FIXED_DT + timedelta(seconds=1),
            ),
        ])
        result = m5_service.run_learning_models(batch)
        # concept_1: only obs_1 → 4/5 = 0.8
        assert abs(result.diagnosis.concept_mastery["concept_1"] - 0.8) < 1e-9
        # concept_2: avg(4/5, 5/5) = 0.9
        assert abs(result.diagnosis.concept_mastery["concept_2"] - 0.9) < 1e-9

    def test_bkt_correct_increases_probability(self, m5_service):
        """正确作答后 BKT 概率应高于初始值 P(L0)=0.1。"""
        batch = _make_batch([
            LearningObservation(
                observation_id="obs_1",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_1",
                item_version="1.0.0",
                concept_ids=["concept_1"],
                score=5.0,
                max_score=5.0,
                source_audit_id="audit_1",
                source_audit_version=1,
                occurred_at=FIXED_DT,
            ),
        ])
        result = m5_service.run_learning_models(batch)
        prob = result.knowledge_trace.concept_probabilities["concept_1"]
        assert 0.0 <= prob <= 1.0
        assert prob > 0.1  # 高于初始 P(L0)

    def test_bkt_incorrect_lower_than_correct(self, m5_service):
        """错误作答的概率应低于正确作答的概率。"""
        def _run(score: float) -> float:
            batch = _make_batch([
                LearningObservation(
                    observation_id="obs_1",
                    learner_id="learner_1",
                    course_id="course_1",
                    class_id="class_1",
                    attempt_id="attempt_1",
                    item_id="item_1",
                    item_version="1.0.0",
                    concept_ids=["concept_1"],
                    score=score,
                    max_score=5.0,
                    source_audit_id="audit_1",
                    source_audit_version=1,
                    occurred_at=FIXED_DT,
                ),
            ])
            result = m5_service.run_learning_models(batch)
            return result.knowledge_trace.concept_probabilities["concept_1"]

        prob_correct = _run(5.0)
        prob_incorrect = _run(0.0)
        assert 0.0 <= prob_incorrect <= 1.0
        assert 0.0 <= prob_correct <= 1.0
        # 正确作答的概率应高于错误作答
        assert prob_correct > prob_incorrect

    def test_bkt_watermark_preserved(self, m5_service, observation_batch):
        """BKT 快照应保留观测水位。"""
        result = m5_service.run_learning_models(observation_batch)
        assert (
            result.knowledge_trace.observation_watermark
            == observation_batch.watermark
        )

    def test_model_version_not_unconfigured(self, m5_service, observation_batch):
        """估计状态下 model_version 不应为 'unconfigured'。"""
        result = m5_service.run_learning_models(observation_batch)
        assert result.diagnosis.model_version != "unconfigured"
        assert result.knowledge_trace.model_version != "unconfigured"
