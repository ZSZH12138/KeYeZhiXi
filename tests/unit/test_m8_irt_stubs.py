"""M8 IRT 空壳方法验证测试。"""

from datetime import datetime, timedelta, timezone

from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    CalibrationRunResult,
    AdaptiveSelectionResult,
)


FIXED_DT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class TestM8IRTStubs:
    def test_calibrate_irt_empty(self, m8_service, observation_batch):
        """IRT 标定空壳返回 status='empty', converged=False。"""
        result = m8_service.calibrate_irt(observation_batch, FIXED_DT)
        assert isinstance(result, CalibrationRunResult)
        assert result.status == "empty"
        assert result.converged is False
        assert result.parameter_set.status == "empty"
        assert result.parameter_set.item_parameters == []
        assert result.metrics == {}

    def test_select_adaptive_empty(self, m8_service, observation_batch):
        """自适应选题空壳返回 status='empty', item_ids=[]。"""
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
