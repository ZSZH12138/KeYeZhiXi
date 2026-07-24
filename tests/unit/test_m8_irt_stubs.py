"""M8 IRT 标定和自适应选题算法框架验证测试。"""

from datetime import datetime, timedelta, timezone

from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    AdaptiveSelectionResult,
    CalibrationRunResult,
    IRTParameterSet,
    LearningObservation,
    LearningObservationBatch,
)


FIXED_DT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


def _make_observation_batch(
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


class TestM8IRTCalibrationEmpty:
    """无观测时 IRT 标定应返回 empty。"""

    def test_calibrate_irt_empty(self, m8_service):
        """空批次 → status='empty', converged=False。"""
        batch = _make_observation_batch([])
        result = m8_service.calibrate_irt(batch, FIXED_DT)
        assert isinstance(result, CalibrationRunResult)
        assert result.status == "empty"
        assert result.converged is False
        assert result.parameter_set.status == "empty"
        assert result.parameter_set.item_parameters == []
        assert result.metrics == {}


class TestM8IRTCalibrationShadow:
    """有观测时 IRT 标定应返回 shadow。"""

    def test_calibrate_irt_shadow_status(self, m8_service, observation_batch):
        """有观测 → status='shadow', converged=True。"""
        result = m8_service.calibrate_irt(observation_batch, FIXED_DT)
        assert isinstance(result, CalibrationRunResult)
        assert result.status == "shadow"
        assert result.converged is True
        assert result.parameter_set.status == "shadow"

    def test_irt_has_item_parameters(self, m8_service, observation_batch):
        """标定结果应包含逐题参数。"""
        result = m8_service.calibrate_irt(observation_batch, FIXED_DT)
        assert len(result.parameter_set.item_parameters) > 0
        params = result.parameter_set.item_parameters[0]
        assert params.item_id == "item_1_1"
        assert params.item_version == "1.0.0"
        assert params.discrimination > 0.0
        assert params.sample_size >= 1

    def test_irt_difficulty_correct_for_full_score(self, m8_service):
        """满分 → p-value=1.0 → difficulty 为负（简单题）。"""
        batch = _make_observation_batch([
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
        result = m8_service.calibrate_irt(batch, FIXED_DT)
        params = result.parameter_set.item_parameters[0]
        # p-value clamped to 0.99 → difficulty = ln(0.01/0.99) < 0
        assert params.difficulty < 0

    def test_irt_difficulty_correct_for_zero_score(self, m8_service):
        """零分 → p-value=0.0 → difficulty 为正（难题）。"""
        batch = _make_observation_batch([
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
        result = m8_service.calibrate_irt(batch, FIXED_DT)
        params = result.parameter_set.item_parameters[0]
        # p-value clamped to 0.01 → difficulty = ln(0.99/0.01) > 0
        assert params.difficulty > 0

    def test_irt_metrics_populated(self, m8_service, observation_batch):
        """shadow 状态应包含基础指标。"""
        result = m8_service.calibrate_irt(observation_batch, FIXED_DT)
        assert "mean_p_value" in result.metrics
        assert "response_variance" in result.metrics
        assert "item_count" in result.metrics
        assert result.metrics["item_count"] >= 1.0

    def test_irt_sample_size_matches(self, m8_service, observation_batch):
        """sample_size 应等于观测数。"""
        result = m8_service.calibrate_irt(observation_batch, FIXED_DT)
        assert result.parameter_set.sample_size == len(
            observation_batch.observations
        )


class TestM8AdaptiveSelectionEmpty:
    """策略未配置或能力未估计时应返回 empty。"""

    def test_select_adaptive_empty_policy(self, m8_service):
        """策略 status='empty' → 返回 empty。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_1",
            version="1.0.0",
            parameter_set_id=None,
            max_items=5,
            concept_quotas={"concept_1": 3},
            status="empty",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_1",
            learner_id="learner_1",
            parameter_set_id="param_1",
            theta=None,
            standard_error=None,
            status="empty",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert isinstance(result, AdaptiveSelectionResult)
        assert result.status == "empty"
        assert result.item_ids == []
        assert result.ability_estimate is None

    def test_select_adaptive_empty_ability(self, m8_service):
        """策略已配置但能力 status='empty' → 返回 empty。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_2",
            version="1.0.0",
            parameter_set_id="param_set_1",
            max_items=5,
            concept_quotas={"concept_1": 3},
            status="configured",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_2",
            learner_id="learner_1",
            parameter_set_id="param_set_1",
            theta=None,
            standard_error=None,
            status="empty",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert result.status == "empty"
        assert result.ability_estimate is None


class TestM8AdaptiveSelectionSelected:
    """策略已配置且能力已估计时应返回 selected。"""

    def test_select_adaptive_selected_status(self, m8_service):
        """策略 configured + 能力 estimated → 返回 selected。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_3",
            version="1.0.0",
            parameter_set_id="param_set_1",
            max_items=5,
            concept_quotas={"concept_1": 3, "concept_2": 2},
            status="configured",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_3",
            learner_id="learner_1",
            parameter_set_id="param_set_1",
            theta=0.5,
            standard_error=0.3,
            status="estimated",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert isinstance(result, AdaptiveSelectionResult)
        assert result.status == "selected"

    def test_select_adaptive_preserves_learner(self, m8_service):
        """返回结果应保留学习者身份。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_4",
            version="1.0.0",
            parameter_set_id="param_set_1",
            max_items=5,
            concept_quotas={"concept_1": 3},
            status="configured",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_4",
            learner_id="learner_42",
            parameter_set_id="param_set_1",
            theta=1.0,
            standard_error=0.5,
            status="estimated",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert result.learner_id == "learner_42"

    def test_select_adaptive_passes_ability(self, m8_service):
        """selected 状态应传递能力估计。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_5",
            version="1.0.0",
            parameter_set_id="param_set_1",
            max_items=5,
            concept_quotas={"concept_1": 3},
            status="configured",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_5",
            learner_id="learner_1",
            parameter_set_id="param_set_1",
            theta=0.8,
            standard_error=0.2,
            status="estimated",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert result.ability_estimate is not None
        assert result.ability_estimate.theta == 0.8
        assert result.ability_estimate.status == "estimated"

    def test_select_adaptive_preserves_policy_id(self, m8_service):
        """返回结果应保留策略 ID。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_6",
            version="1.0.0",
            parameter_set_id="param_set_1",
            max_items=3,
            concept_quotas={"concept_1": 2},
            status="configured",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_6",
            learner_id="learner_1",
            parameter_set_id="param_set_1",
            theta=0.0,
            standard_error=1.0,
            status="estimated",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert result.policy_id == "policy_6"
