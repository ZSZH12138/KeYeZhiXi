"""M5 run_learning_models 空壳验证测试。"""

from course_insight.contracts.learning_models import LearningModelRun


class TestM5LearningModels:
    def test_run_learning_models_empty(self, m5_service, observation_batch):
        """空壳方法应返回 status='empty'。"""
        result = m5_service.run_learning_models(observation_batch)
        assert isinstance(result, LearningModelRun)
        assert result.status == "empty"
        assert result.diagnosis.status == "empty"
        assert result.knowledge_trace.status == "empty"

    def test_run_learning_models_preserves_learner(self, m5_service, observation_batch):
        """返回结果的 learner_id 应与输入一致。"""
        result = m5_service.run_learning_models(observation_batch)
        assert result.diagnosis.learner_id == observation_batch.learner_id
        assert result.knowledge_trace.learner_id == observation_batch.learner_id

    def test_run_learning_models_preserves_count(self, m5_service, observation_batch):
        """observation_count 应等于输入的观测数。"""
        result = m5_service.run_learning_models(observation_batch)
        assert result.observation_count == len(observation_batch.observations)
        assert result.diagnosis.observation_count == len(observation_batch.observations)
        assert result.knowledge_trace.observation_count == len(observation_batch.observations)
