"""M8 generate_paper 单元测试。"""

import pytest
from course_insight.contracts.assessment import AssessmentPaper
from course_insight.contracts.errors import DomainError
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m8_assessment_scoring.paper_generator import FIXED_TIME


class TestM8GeneratePaper:
    def test_generate_paper_success(self, m8_service, task_plan, knowledge_bundle):
        """正常组卷，返回冻结的 AssessmentPaper。"""
        paper = m8_service.generate_paper(task_plan, knowledge_bundle, None, None)
        assert isinstance(paper, AssessmentPaper)
        assert paper.paper_id == "paper_task_1"
        assert paper.task_id == "task_1"
        assert paper.immutable_checksum == paper.freeze()

    def test_generate_paper_total_score(self, m8_service, task_plan, knowledge_bundle):
        """试卷总分等于蓝图总分。"""
        paper = m8_service.generate_paper(task_plan, knowledge_bundle, None, None)
        blueprint = knowledge_bundle.get_blueprint(task_plan.blueprint_id)
        assert abs(paper.total_score() - blueprint.total_score) < 1e-9

    def test_generate_paper_items_count(self, m8_service, task_plan, knowledge_bundle):
        """每个 section 的题目数等于 BlueprintSection.item_count。"""
        paper = m8_service.generate_paper(task_plan, knowledge_bundle, None, None)
        blueprint = knowledge_bundle.get_blueprint(task_plan.blueprint_id)
        for section, bp_section in zip(paper.sections, blueprint.sections):
            assert len(section.items) == bp_section.item_count

    def test_generate_paper_no_blueprint(self, m8_service, task_plan, knowledge_bundle):
        """TaskPlan 指向不存在的蓝图时抛出 BLUEPRINT_NOT_FOUND。"""
        bad_task = task_plan.model_copy(update={"blueprint_id": "nonexistent"})
        with pytest.raises(DomainError) as exc_info:
            m8_service.generate_paper(bad_task, knowledge_bundle, None, None)
        assert exc_info.value.code == "BLUEPRINT_NOT_FOUND"

    def test_generate_paper_blueprint_not_approved(self, m8_service, task_plan, knowledge_bundle):
        """蓝图未审批时抛出 BLUEPRINT_UNSATISFIABLE。"""
        bad_bundle = knowledge_bundle.model_copy(deep=True)
        object.__setattr__(bad_bundle.blueprints[0], "status", "draft")
        with pytest.raises(DomainError) as exc_info:
            m8_service.generate_paper(task_plan, bad_bundle, None, None)
        assert exc_info.value.code == "BLUEPRINT_UNSATISFIABLE"
