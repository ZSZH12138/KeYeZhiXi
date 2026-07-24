"""M8 finalize_scoring 单元测试。"""

import pytest
from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError


class TestM8FinalizeScoring:
    def test_finalize_scoring_success(self, m8_service, scoring_preparation, rubric_scoring_results):
        """正常合并评分。"""
        bundle = m8_service.finalize_scoring(scoring_preparation, rubric_scoring_results)
        assert isinstance(bundle, ScoringResultBundle)
        assert bundle.attempt_id == "attempt_1"
        assert bundle.learner_id == "learner_1"
        assert len(bundle.score_audit_records) == 2  # 1 客观 + 1 主观

    def test_finalize_scoring_total(self, m8_service, scoring_preparation, rubric_scoring_results):
        """总分等于各审计记录之和。"""
        bundle = m8_service.finalize_scoring(scoring_preparation, rubric_scoring_results)
        expected_total = sum(a.total_score for a in bundle.score_audit_records)
        assert abs(bundle.total_score - expected_total) < 1e-9

    def test_finalize_scoring_max(self, m8_service, scoring_preparation, rubric_scoring_results):
        """max_score 等于各审计记录 max_score 之和。"""
        bundle = m8_service.finalize_scoring(scoring_preparation, rubric_scoring_results)
        expected_max = sum(a.max_score for a in bundle.score_audit_records)
        assert abs(bundle.max_score - expected_max) < 1e-9

    def test_finalize_scoring_mismatch(self, m8_service, scoring_preparation):
        """量规结果与任务不匹配时抛出 SCORING_TASK_RESULT_MISMATCH。"""
        from course_insight.contracts.assessment import RubricScoringResult
        bad_results = [
            RubricScoringResult(
                scoring_task_id="nonexistent_task",
                criterion_scores=[],
                total_score=0.0,
                confidence=0.5,
                missing_concept_ids=[],
                review_flags=[],
                model_name="local_model",
                model_version="1.0.0",
                scored_at=scoring_preparation.prepared_at,
            ),
        ]
        with pytest.raises(DomainError) as exc_info:
            m8_service.finalize_scoring(scoring_preparation, bad_results)
        assert exc_info.value.code == "SCORING_TASK_RESULT_MISMATCH"

    def test_finalize_scoring_remediation(self, m8_service, scoring_preparation):
        """主观题缺少概念时，补救计划包含缺失概念。"""
        from course_insight.contracts.assessment import (
            CriterionScore,
            RubricScoringResult,
        )
        task_id = scoring_preparation.rubric_scoring_tasks[0].scoring_task_id
        results = [
            RubricScoringResult(
                scoring_task_id=task_id,
                criterion_scores=[
                    CriterionScore(
                        criterion_id="criterion_1",
                        score=0.0,
                        student_evidence="",
                        course_evidence_id=None,
                        reason="学生未作答",
                    ),
                    CriterionScore(
                        criterion_id="criterion_2",
                        score=0.0,
                        student_evidence="",
                        course_evidence_id=None,
                        reason="学生未作答",
                    ),
                ],
                total_score=0.0,
                confidence=0.3,
                missing_concept_ids=["concept_2"],
                review_flags=["low_confidence"],
                model_name="local_model",
                model_version="1.0.0",
                scored_at=scoring_preparation.prepared_at,
            ),
        ]
        bundle = m8_service.finalize_scoring(scoring_preparation, results)
        assert len(bundle.remediation_plan.targets) > 0
        assert bundle.remediation_plan.targets[0].concept_id == "concept_2"
