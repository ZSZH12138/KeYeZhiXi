"""M8 -> M5 完整链路集成测试。"""

import pytest
from course_insight.contracts.analytics import (
    CriterionOverride,
    TeacherReviewDecision,
)
from course_insight.contracts.assessment import (
    CriterionScore,
    RubricScoringResult,
)
from course_insight.contracts.state import StateUpdateResult


class TestM8M5Chain:
    def test_m8_generate_to_m5_update(
        self,
        m8_service,
        m5_service,
        task_plan,
        knowledge_bundle,
        raw_answer_path,
        state_policy_path,
    ):
        """完整链路：M8 组卷 -> 评分 -> M5 更新状态。"""
        # 1. M8 组卷
        paper = m8_service.generate_paper(task_plan, knowledge_bundle, None, None)

        # 2. M8 评分准备
        preparation = m8_service.prepare_scoring(paper, raw_answer_path, knowledge_bundle)

        # 3. 构造主观题评分结果
        rubric_results = [
            RubricScoringResult(
                scoring_task_id=preparation.rubric_scoring_tasks[0].scoring_task_id,
                criterion_scores=[
                    CriterionScore(
                        criterion_id="criterion_1",
                        score=3.0,
                        student_evidence="设x为未知数",
                        course_evidence_id="evidence_1",
                        reason="正确列出方程",
                    ),
                    CriterionScore(
                        criterion_id="criterion_2",
                        score=2.0,
                        student_evidence="x=5",
                        course_evidence_id="evidence_2",
                        reason="正确求解",
                    ),
                ],
                total_score=5.0,
                confidence=0.9,
                missing_concept_ids=[],
                review_flags=[],
                model_name="local_model",
                model_version="1.0.0",
                scored_at=preparation.prepared_at,
            ),
        ]

        # 4. M8 最终评分
        bundle = m8_service.finalize_scoring(preparation, rubric_results)

        # 5. M5 更新状态
        result = m5_service.update_state(
            bundle, knowledge_bundle, None, None, state_policy_path,
        )
        assert isinstance(result, StateUpdateResult)
        assert result.learner_state_snapshot.state_version == 1

    def test_m8_review_to_m5_recompute(
        self,
        m8_service,
        m5_service,
        scoring_result_bundle,
        knowledge_bundle,
        state_policy_path,
    ):
        """M8 教师复核 -> M5 重算状态，新版本 state_version 递增。"""
        # 1. 第一次 M5 更新
        first_result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        assert first_result.learner_state_snapshot.state_version == 1

        # 2. 教师确认审计
        audit = scoring_result_bundle.score_audit_records[0]
        decision = TeacherReviewDecision(
            decision_id="decision_1",
            audit_id=audit.audit_id,
            expected_audit_version=audit.audit_version,
            decision="confirm",
            final_total_score=audit.total_score,
            criterion_overrides=[],
            teacher_comment="确认无误",
            reviewer_id="teacher_1",
            reviewed_at=audit.created_at,
        )
        reviewed_bundle = m8_service.apply_teacher_review(scoring_result_bundle, decision)

        # 3. 用新 M5 实例（避免 _processed_by_learner 缓存）再做 M5 更新
        from course_insight.modules.m5_learner_class_state.stubs import M5StateServiceStub
        new_m5 = M5StateServiceStub()
        second_result = new_m5.update_state(
            reviewed_bundle,
            knowledge_bundle,
            first_result.learner_state_snapshot,
            first_result.class_state_snapshot,
            state_policy_path,
        )
        assert second_result.learner_state_snapshot.state_version == 2
