"""M5 班级聚合策略测试。"""

from course_insight.contracts.state import ClassStateSnapshot
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
    StatePolicy,
)


class TestM5Aggregation:
    def test_aggregate_single_learner(
        self,
        m5_service,
        scoring_result_bundle,
        knowledge_bundle,
        state_policy_path,
    ):
        """单学习者聚合，ClassStateSnapshot 字段正确。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        class_state = result.class_state_snapshot
        assert isinstance(class_state, ClassStateSnapshot)
        assert class_state.class_id == "class_1"
        assert class_state.class_size == 1
        assert class_state.assessed_count == 1
        assert len(class_state.concept_status) == len(knowledge_bundle.concepts)

    def test_aggregate_coverage(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """覆盖率计算正确：1 个学生 / 班级 1 人 = 1.0。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert result.class_state_snapshot.coverage_rate == 1.0

    def test_aggregate_evidence_sufficient(
        self,
        m5_service,
        scoring_result_bundle,
        knowledge_bundle,
        state_policy_path,
    ):
        """minimum_assessed_count=1, coverage=1.0 -> sufficient。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert result.class_state_snapshot.evidence_status == "sufficient"
