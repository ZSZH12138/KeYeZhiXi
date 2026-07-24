"""M5 诊断策略和状态更新策略测试。"""

import json

import pytest
from course_insight.contracts.errors import DomainError
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
    StatePolicy,
    audit_version_key,
    latest_audits,
)


class TestStatePolicy:
    def test_state_policy_from_path(self, state_policy_path):
        """从 JSON 加载策略，字段正确。"""
        policy = StatePolicy.from_path(state_policy_path)
        assert policy.class_id == "class_1"
        assert policy.class_size == 1
        assert policy.mastered_threshold == 0.8
        assert policy.consolidating_threshold == 0.4

    def test_state_policy_invalid_missing_field(self, tmp_path):
        """缺少字段时抛出 STATE_POLICY_INVALID。"""
        path = tmp_path / "bad_policy.json"
        path.write_text(json.dumps({"class_id": "class_1"}), encoding="utf-8")
        with pytest.raises(DomainError) as exc_info:
            StatePolicy.from_path(path)
        assert exc_info.value.code == "STATE_POLICY_INVALID"

    def test_state_policy_invalid_threshold(self, tmp_path):
        """阈值越界时抛出 STATE_POLICY_INVALID。"""
        data = {
            "aggregation_policy_version": "1.0.0",
            "class_id": "class_1",
            "class_size": 1,
            "consolidating_threshold": 0.9,
            "mastered_threshold": 0.8,
            "minimum_assessed_count": 1,
            "minimum_coverage": 0.0,
            "misconception_activation_threshold": 0.5,
        }
        path = tmp_path / "bad_threshold.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(DomainError) as exc_info:
            StatePolicy.from_path(path)
        assert exc_info.value.code == "STATE_POLICY_INVALID"


class TestAuditHelpers:
    def test_audit_version_key(self, scoring_result_bundle):
        """audit_version_key 格式为 audit_id:audit_version。"""
        record = scoring_result_bundle.score_audit_records[0]
        key = audit_version_key(record)
        assert key == f"{record.audit_id}:{record.audit_version}"

    def test_latest_audits_dedup(self, scoring_result_bundle):
        """同一 audit_id 多版本时只保留最新。"""
        audits = latest_audits(scoring_result_bundle)
        # 每个 audit_id 只出现一次
        ids = [a.audit_id for a in audits]
        assert len(ids) == len(set(ids))


class TestDeterministicStateUpdatePolicy:
    def test_build_diagnosis(self, scoring_result_bundle, knowledge_bundle):
        """构建诊断结果，字段正确。"""
        policy = DeterministicStateUpdatePolicy()
        diagnosis = policy.build_diagnosis(scoring_result_bundle, knowledge_bundle)
        assert diagnosis.learner_id == scoring_result_bundle.learner_id
        assert diagnosis.attempt_id == scoring_result_bundle.attempt_id
        assert len(diagnosis.item_diagnoses) > 0

    def test_build_learner_state(self, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """构建学习者状态，字段正确。"""
        update_policy = DeterministicStateUpdatePolicy()
        diagnosis = update_policy.build_diagnosis(scoring_result_bundle, knowledge_bundle)
        state_policy = StatePolicy.from_path(state_policy_path)
        learner = update_policy.build_learner_state(
            scoring_result_bundle,
            knowledge_bundle,
            diagnosis,
            None,
            state_policy,
        )
        assert learner.learner_id == scoring_result_bundle.learner_id
        assert learner.state_version == 1
        assert len(learner.concept_states) == len(knowledge_bundle.concepts)
