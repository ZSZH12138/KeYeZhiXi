"""M8 apply_teacher_review 单元测试。"""

import pytest
from course_insight.contracts.analytics import (
    CriterionOverride,
    TeacherReviewDecision,
)
from course_insight.contracts.errors import DomainError


class TestM8TeacherReview:
    def _make_confirm_decision(self, bundle, audit_id) -> TeacherReviewDecision:
        audit = bundle.get_audit_record(audit_id)
        return TeacherReviewDecision(
            decision_id="decision_1",
            audit_id=audit_id,
            expected_audit_version=audit.audit_version,
            decision="confirm",
            final_total_score=audit.total_score,
            criterion_overrides=[],
            teacher_comment="确认无误",
            reviewer_id="teacher_1",
            reviewed_at=audit.created_at,
        )

    def test_teacher_review_approve(self, m8_service, scoring_result_bundle):
        """教师确认：audit_version 递增，status 变为 approved。"""
        audit = scoring_result_bundle.score_audit_records[0]
        decision = self._make_confirm_decision(scoring_result_bundle, audit.audit_id)
        reviewed = m8_service.apply_teacher_review(scoring_result_bundle, decision)
        new_audit = reviewed.get_audit_record(audit.audit_id)
        assert new_audit.audit_version == audit.audit_version + 1
        assert new_audit.review_status == "approved"

    def test_teacher_review_reject(self, m8_service, scoring_result_bundle):
        """教师拒绝：status 变为 rejected。"""
        audit = scoring_result_bundle.score_audit_records[0]
        decision = TeacherReviewDecision(
            decision_id="decision_1",
            audit_id=audit.audit_id,
            expected_audit_version=audit.audit_version,
            decision="reject",
            final_total_score=audit.total_score,
            criterion_overrides=[],
            teacher_comment="答案有误",
            reviewer_id="teacher_1",
            reviewed_at=audit.created_at,
        )
        reviewed = m8_service.apply_teacher_review(scoring_result_bundle, decision)
        new_audit = reviewed.get_audit_record(audit.audit_id)
        assert new_audit.review_status == "rejected"

    def test_teacher_review_override(self, m8_service, scoring_result_bundle, knowledge_bundle):
        """教师覆盖分数：分数被替换，method 变为 teacher_override。"""
        # 找到主观题审计记录（有 criterion_scores 的那条）
        subjective_audit = None
        for record in scoring_result_bundle.score_audit_records:
            if len(record.criterion_scores) > 1:
                subjective_audit = record
                break
        assert subjective_audit is not None, "应有主观题审计记录"

        # 从知识包中查找 rubric，获取每个 criterion 的 max_score
        rubric = knowledge_bundle.rubrics[0]
        criterion_max = {c.criterion_id: c.max_score for c in rubric.criteria}

        overrides = [
            CriterionOverride(
                criterion_id=score.criterion_id,
                previous_score=score.score,
                new_score=criterion_max.get(score.criterion_id, score.score),
                reason="教师调整",
            )
            for score in subjective_audit.criterion_scores
        ]
        new_total = sum(o.new_score for o in overrides)
        decision = TeacherReviewDecision(
            decision_id="decision_1",
            audit_id=subjective_audit.audit_id,
            expected_audit_version=subjective_audit.audit_version,
            decision="override",
            final_total_score=new_total,
            criterion_overrides=overrides,
            teacher_comment="调整分数",
            reviewer_id="teacher_1",
            reviewed_at=subjective_audit.created_at,
        )
        reviewed = m8_service.apply_teacher_review(scoring_result_bundle, decision)
        new_audit = reviewed.get_audit_record(subjective_audit.audit_id)
        assert new_audit.scoring_method == "teacher_override"
        assert abs(new_audit.total_score - new_total) < 1e-9

    def test_teacher_review_version_conflict(self, m8_service, scoring_result_bundle):
        """版本不匹配时抛出 REVIEW_VERSION_CONFLICT。"""
        audit = scoring_result_bundle.score_audit_records[0]
        decision = TeacherReviewDecision(
            decision_id="decision_1",
            audit_id=audit.audit_id,
            expected_audit_version=999,  # 错误版本
            decision="confirm",
            final_total_score=audit.total_score,
            criterion_overrides=[],
            teacher_comment="确认",
            reviewer_id="teacher_1",
            reviewed_at=audit.created_at,
        )
        with pytest.raises(DomainError) as exc_info:
            m8_service.apply_teacher_review(scoring_result_bundle, decision)
        assert exc_info.value.code == "REVIEW_VERSION_CONFLICT"
