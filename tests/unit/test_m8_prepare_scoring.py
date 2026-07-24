"""M8 prepare_scoring 单元测试。"""

import pytest
from course_insight.contracts.assessment import ScoringPreparationResult
from course_insight.contracts.errors import DomainError


class TestM8PrepareScoring:
    def test_prepare_scoring_success(self, m8_service, assessment_paper, raw_answer_path, knowledge_bundle):
        """正常准备评分。"""
        result = m8_service.prepare_scoring(assessment_paper, raw_answer_path, knowledge_bundle)
        assert isinstance(result, ScoringPreparationResult)
        assert result.attempt_id == "attempt_1"
        assert result.paper_id == "paper_task_1"
        assert result.learner_id == "learner_1"

    def test_prepare_scoring_objective(self, m8_service, assessment_paper, raw_answer_path, knowledge_bundle):
        """客观题在 objective_audit_records 中。"""
        result = m8_service.prepare_scoring(assessment_paper, raw_answer_path, knowledge_bundle)
        assert len(result.objective_audit_records) == 1
        audit = result.objective_audit_records[0]
        assert audit.scoring_method == "rule"
        assert audit.item_instance_id == "item_1_instance_1"

    def test_prepare_scoring_subjective(self, m8_service, assessment_paper, raw_answer_path, knowledge_bundle):
        """主观题在 rubric_scoring_tasks 中。"""
        result = m8_service.prepare_scoring(assessment_paper, raw_answer_path, knowledge_bundle)
        assert len(result.rubric_scoring_tasks) == 1
        task = result.rubric_scoring_tasks[0]
        assert task.item_instance.item_instance_id == "item_2_instance_1"
        assert task.rubric.rubric_id == "rubric_1"

    def test_prepare_scoring_missing_item(self, m8_service, assessment_paper, raw_answer_missing_path, knowledge_bundle):
        """答案缺少题目时抛出 ANSWER_FORMAT_INVALID。"""
        with pytest.raises(DomainError) as exc_info:
            m8_service.prepare_scoring(assessment_paper, raw_answer_missing_path, knowledge_bundle)
        assert exc_info.value.code == "ANSWER_FORMAT_INVALID"

    def test_prepare_scoring_extra_item(self, m8_service, assessment_paper, raw_answer_extra_path, knowledge_bundle):
        """答案多出题目时抛出 ANSWER_FORMAT_INVALID。"""
        with pytest.raises(DomainError) as exc_info:
            m8_service.prepare_scoring(assessment_paper, raw_answer_extra_path, knowledge_bundle)
        assert exc_info.value.code == "ANSWER_FORMAT_INVALID"

    def test_prepare_scoring_bad_json(self, m8_service, assessment_paper, raw_answer_bad_json_path, knowledge_bundle):
        """JSON 格式错误时抛出 ANSWER_FORMAT_INVALID。"""
        with pytest.raises(DomainError) as exc_info:
            m8_service.prepare_scoring(assessment_paper, raw_answer_bad_json_path, knowledge_bundle)
        assert exc_info.value.code == "ANSWER_FORMAT_INVALID"
