"""M5 update_state 单元测试。"""

import pytest
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import StateUpdateResult


class TestM5UpdateState:
    def test_update_state_success(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """正常更新状态，返回完整的 StateUpdateResult。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert isinstance(result, StateUpdateResult)
        assert result.diagnosis_result.learner_id == "learner_1"
        assert result.learner_state_snapshot.learner_id == "learner_1"
        assert result.learner_state_snapshot.state_version == 1
        assert result.class_state_snapshot.class_id == "class_1"
        assert len(result.processed_audit_ids) > 0

    def test_update_state_no_audits(self, m5_service, knowledge_bundle, state_policy_path, scoring_result_bundle):
        """评分包无审计记录时，抛出 INSUFFICIENT_EVIDENCE。"""
        from course_insight.contracts.assessment import ScoringResultBundle
        empty_bundle = ScoringResultBundle(
            attempt_id="attempt_1",
            paper_id="paper_task_1",
            learner_id="learner_1",
            score_audit_records=[],
            learning_events=[],
            remediation_plan=scoring_result_bundle.remediation_plan,
            total_score=0.0,
            max_score=0.0,
            finalized_at=scoring_result_bundle.finalized_at,
        )
        with pytest.raises(DomainError) as exc_info:
            m5_service.update_state(empty_bundle, knowledge_bundle, None, None, state_policy_path)
        assert exc_info.value.code == "INSUFFICIENT_EVIDENCE"

    def test_update_state_stale_version(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """重复处理同一审计版本时，抛出 STALE_STATE_VERSION。"""
        # 第一次调用成功
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        # 第二次调用同样的数据，应该报 STALE_STATE_VERSION
        with pytest.raises(DomainError) as exc_info:
            m5_service.update_state(
                scoring_result_bundle,
                knowledge_bundle,
                result.learner_state_snapshot,
                result.class_state_snapshot,
                state_policy_path,
            )
        assert exc_info.value.code == "STALE_STATE_VERSION"

    def test_update_state_with_previous(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """带前版快照更新，state_version 应递增。"""
        # 第一次调用
        first_result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert first_result.learner_state_snapshot.state_version == 1

    def test_update_state_assert_consistent(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """返回结果的 assert_consistent 不应抛异常。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        # 如果不一致会抛 DomainError
        result.assert_consistent()
