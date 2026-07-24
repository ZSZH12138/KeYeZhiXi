"""M8 PaperGenerator 组卷器测试。"""

import pytest
from course_insight.contracts.assessment import AssessmentPaper
from course_insight.contracts.errors import DomainError
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator


class TestPaperGenerator:
    def test_paper_generator_freeze(self, task_plan, knowledge_bundle):
        """冻结试卷的 immutable_checksum 与 freeze() 一致。"""
        generator = PaperGenerator()
        paper = generator.generate(task_plan, knowledge_bundle, None, None)
        assert isinstance(paper, AssessmentPaper)
        assert paper.immutable_checksum == paper.freeze()

    def test_paper_generator_section_score(self, task_plan, knowledge_bundle):
        """每个 section 的 score 等于其题目 max_score 之和。"""
        generator = PaperGenerator()
        paper = generator.generate(task_plan, knowledge_bundle, None, None)
        for section in paper.sections:
            expected = sum(item.max_score for item in section.items)
            assert abs(section.score - expected) < 1e-9

    def test_paper_generator_total_matches_blueprint(self, task_plan, knowledge_bundle):
        """试卷总分等于蓝图总分。"""
        generator = PaperGenerator()
        paper = generator.generate(task_plan, knowledge_bundle, None, None)
        blueprint = knowledge_bundle.get_blueprint(task_plan.blueprint_id)
        assert abs(paper.total_score() - blueprint.total_score) < 1e-9
