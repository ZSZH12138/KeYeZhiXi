"""M8 RuleScorer 客观题评分测试。"""

from course_insight.contracts.assessment import ScoreAuditRecord
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer


class TestRuleScorer:
    def test_rule_scorer_correct(self, objective_item, assessment_paper, knowledge_bundle):
        """答案正确时得满分。"""
        scorer = RuleScorer()
        instance = assessment_paper.all_items()[0]  # item_1_instance_1
        audit = scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=objective_item,
            raw_answer="5",
        )
        assert isinstance(audit, ScoreAuditRecord)
        assert audit.total_score == instance.max_score
        assert audit.scoring_method == "rule"

    def test_rule_scorer_wrong(self, objective_item, assessment_paper):
        """答案错误时得 0 分。"""
        scorer = RuleScorer()
        instance = assessment_paper.all_items()[0]
        audit = scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=objective_item,
            raw_answer="3",
        )
        assert audit.total_score == 0.0

    def test_rule_scorer_normalize(self, objective_item, assessment_paper):
        """答案大小写/空格归一化后比较。"""
        scorer = RuleScorer()
        instance = assessment_paper.all_items()[0]
        # 带空格的 "  5  " 归一化后应等于 "5"
        audit = scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=objective_item,
            raw_answer="  5  ",
        )
        assert audit.total_score == instance.max_score
