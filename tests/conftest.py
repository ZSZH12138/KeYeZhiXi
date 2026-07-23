"""M5/M8 测试全局 fixtures。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    RubricScoringResult,
    ScoreAuditRecord,
    ScoringPreparationResult,
    ScoringResultBundle,
)
from course_insight.contracts.analytics import TeacherReviewDecision
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    MisconceptionTag,
    ParameterRule,
    PrerequisiteRelation,
    QMatrixEntry,
    ReviewPolicy,
    Rubric,
    RubricCriterion,
)
from course_insight.contracts.learning_models import (
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m5_learner_class_state.stubs import M5StateServiceStub
from course_insight.modules.m8_assessment_scoring.stubs import M8AssessmentServiceStub

FIXED_DT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


# ────────────────────── 基础数据 fixtures ──────────────────────


@pytest.fixture
def fixed_dt() -> datetime:
    return FIXED_DT


@pytest.fixture
def concepts() -> list[KnowledgeConcept]:
    return [
        KnowledgeConcept(
            concept_id="concept_1",
            name="线性方程",
            chapter_id="chapter_1",
            description="一元一次方程的解法",
            aliases=[],
            status="published",
        ),
        KnowledgeConcept(
            concept_id="concept_2",
            name="不等式",
            chapter_id="chapter_1",
            description="一元一次不等式",
            aliases=[],
            status="published",
        ),
    ]


@pytest.fixture
def prerequisite_relations() -> list[PrerequisiteRelation]:
    return [
        PrerequisiteRelation(
            from_concept_id="concept_1",
            to_concept_id="concept_2",
            relation_type="prerequisite",
            strength=0.8,
        ),
    ]


@pytest.fixture
def misconception_tags() -> list[MisconceptionTag]:
    return [
        MisconceptionTag(
            misconception_id="misconception_1",
            name="符号错误",
            description="移项时忘记变号",
            concept_ids=["concept_1"],
            evidence_rules=["rule_1"],
        ),
    ]


@pytest.fixture
def rubric_criteria() -> list[RubricCriterion]:
    return [
        RubricCriterion(
            criterion_id="criterion_1",
            description="正确列出方程",
            max_score=3.0,
            expected_student_evidence="设x为未知数",
            course_evidence_ids=["evidence_1"],
        ),
        RubricCriterion(
            criterion_id="criterion_2",
            description="正确求解",
            max_score=2.0,
            expected_student_evidence="x=5",
            course_evidence_ids=["evidence_2"],
        ),
    ]


@pytest.fixture
def review_policy() -> ReviewPolicy:
    return ReviewPolicy(
        low_confidence_threshold=0.5,
        double_score_disagreement_threshold=0.3,
        require_evidence_for_positive_score=True,
    )


@pytest.fixture
def rubric(rubric_criteria, review_policy) -> Rubric:
    return Rubric(
        rubric_id="rubric_1",
        version="1.0.0",
        total_score=5.0,
        criteria=rubric_criteria,
        review_policy=review_policy,
        status="teacher_approved",
    )


@pytest.fixture
def objective_item() -> ItemCard:
    return ItemCard(
        item_id="item_1_1",
        version="1.0.0",
        stem="2x + 3 = 13, 求 x 的值",
        item_type="multiple_choice",
        concept_ids=["concept_1"],
        misconception_ids=["misconception_1"],
        difficulty_level=2,
        cognitive_level="apply",
        parameter_rules=[],
        answer_key={"answer": "5", "max_score": 5.0},
        rubric_id=None,
        source_evidence_ids=["evidence_1"],
        status="teacher_approved",
    )


@pytest.fixture
def subjective_item() -> ItemCard:
    return ItemCard(
        item_id="item_2_1",
        version="1.0.0",
        stem="请解方程 3x - 6 = 9 并写出步骤",
        item_type="short_answer",
        concept_ids=["concept_2"],
        misconception_ids=[],
        difficulty_level=3,
        cognitive_level="analyze",
        parameter_rules=[],
        answer_key={},
        rubric_id="rubric_1",
        source_evidence_ids=["evidence_2"],
        status="teacher_approved",
    )


@pytest.fixture
def blueprint_section() -> BlueprintSection:
    return BlueprintSection(
        section_id="section_1",
        name="基础测试",
        item_count=2,
        score=10.0,
        item_types=[],
        concept_weights={},
        difficulty_range=(0, 5),
        anchor_item_ids=[],
    )


@pytest.fixture
def blueprint(blueprint_section) -> AssessmentBlueprint:
    return AssessmentBlueprint(
        blueprint_id="blueprint_1",
        version="1.0.0",
        course_id="course_1",
        sections=[blueprint_section],
        total_score=10.0,
        duration_minutes=60,
        status="teacher_approved",
    )


@pytest.fixture
def q_matrix() -> list[QMatrixEntry]:
    return [
        QMatrixEntry(
            item_id="item_1_1",
            item_version="1.0.0",
            concept_id="concept_1",
            weight=1.0,
        ),
        QMatrixEntry(
            item_id="item_2_1",
            item_version="1.0.0",
            concept_id="concept_2",
            weight=1.0,
        ),
    ]


@pytest.fixture
def knowledge_bundle(
    concepts,
    prerequisite_relations,
    misconception_tags,
    objective_item,
    subjective_item,
    rubric,
    blueprint,
    q_matrix,
) -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="kb_1",
        course_package_id="cp_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=concepts,
        prerequisite_relations=prerequisite_relations,
        misconception_tags=misconception_tags,
        items=[objective_item, subjective_item],
        rubrics=[rubric],
        blueprints=[blueprint],
        q_matrix=q_matrix,
        status="published",
        published_at=FIXED_DT,
    )


# ────────────────────── TaskPlan ──────────────────────


@pytest.fixture
def task_plan() -> TaskPlan:
    return TaskPlan(
        task_id="task_1",
        task_type="practice",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        blueprint_id="blueprint_1",
        knowledge_bundle_id="kb_1",
        course_package_id="course_package_1",
        workflow=["M8", "M5"],
        next_module="M8",
        created_at=FIXED_DT,
    )


# ────────────────────── StatePolicy JSON ──────────────────────


@pytest.fixture
def state_policy_path(tmp_path) -> Path:
    data = {
        "aggregation_policy_version": "1.0.0",
        "class_id": "class_1",
        "class_size": 1,
        "consolidating_threshold": 0.4,
        "mastered_threshold": 0.8,
        "minimum_assessed_count": 1,
        "minimum_coverage": 0.0,
        "misconception_activation_threshold": 0.5,
    }
    path = tmp_path / "state_policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ────────────────────── 原始答案 JSON ──────────────────────


@pytest.fixture
def raw_answer_path(tmp_path) -> Path:
    """正确答案的 JSON 文件。"""
    data = {
        "attempt_id": "attempt_1",
        "paper_id": "paper_task_1",
        "learner_id": "learner_1",
        "answers": [
            {"item_instance_id": "item_1_instance_1", "answer": "5"},
            {
                "item_instance_id": "item_2_instance_1",
                "answer": "设x为未知数，3x=15，x=5",
            },
        ],
    }
    path = tmp_path / "raw_answers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def raw_answer_wrong_path(tmp_path) -> Path:
    """客观题答案错误的 JSON 文件。"""
    data = {
        "attempt_id": "attempt_1",
        "paper_id": "paper_task_1",
        "learner_id": "learner_1",
        "answers": [
            {"item_instance_id": "item_1_instance_1", "answer": "3"},
            {
                "item_instance_id": "item_2_instance_1",
                "answer": "设x为未知数，3x=15，x=5",
            },
        ],
    }
    path = tmp_path / "raw_answers_wrong.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def raw_answer_missing_path(tmp_path) -> Path:
    """缺少一道题答案的 JSON 文件。"""
    data = {
        "attempt_id": "attempt_1",
        "paper_id": "paper_task_1",
        "learner_id": "learner_1",
        "answers": [
            {"item_instance_id": "item_1_instance_1", "answer": "5"},
        ],
    }
    path = tmp_path / "raw_answers_missing.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def raw_answer_extra_path(tmp_path) -> Path:
    """多出一道题答案的 JSON 文件。"""
    data = {
        "attempt_id": "attempt_1",
        "paper_id": "paper_task_1",
        "learner_id": "learner_1",
        "answers": [
            {"item_instance_id": "item_1_instance_1", "answer": "5"},
            {
                "item_instance_id": "item_2_instance_1",
                "answer": "设x为未知数，3x=15，x=5",
            },
            {"item_instance_id": "item_3_instance_1", "answer": "extra"},
        ],
    }
    path = tmp_path / "raw_answers_extra.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def raw_answer_bad_json_path(tmp_path) -> Path:
    """JSON 格式错误的文件。"""
    path = tmp_path / "raw_answers_bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    return path


# ────────────────────── 服务实例 ──────────────────────


@pytest.fixture
def m5_service() -> M5StateServiceStub:
    return M5StateServiceStub()


@pytest.fixture
def m8_service() -> M8AssessmentServiceStub:
    return M8AssessmentServiceStub()


# ────────────────────── 组合 fixtures ──────────────────────


@pytest.fixture
def assessment_paper(m8_service, task_plan, knowledge_bundle) -> AssessmentPaper:
    """M8 生成的冻结试卷。"""
    return m8_service.generate_paper(task_plan, knowledge_bundle, None, None)


@pytest.fixture
def scoring_preparation(m8_service, assessment_paper, raw_answer_path, knowledge_bundle) -> ScoringPreparationResult:
    """M8 评分准备结果。"""
    return m8_service.prepare_scoring(assessment_paper, raw_answer_path, knowledge_bundle)


@pytest.fixture
def rubric_scoring_results(scoring_preparation) -> list[RubricScoringResult]:
    """主观题满分评分结果。"""
    return [
        RubricScoringResult(
            scoring_task_id=scoring_preparation.rubric_scoring_tasks[0].scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion_1",
                    score=3.0,
                    student_evidence="设x为未知数",
                    course_evidence_id="evidence_1",
                    reason="学生正确列出了方程",
                ),
                CriterionScore(
                    criterion_id="criterion_2",
                    score=2.0,
                    student_evidence="x=5",
                    course_evidence_id="evidence_2",
                    reason="学生正确求解",
                ),
            ],
            total_score=5.0,
            confidence=0.9,
            missing_concept_ids=[],
            review_flags=[],
            model_name="local_model",
            model_version="1.0.0",
            scored_at=FIXED_DT,
        ),
    ]


@pytest.fixture
def scoring_result_bundle(m8_service, scoring_preparation, rubric_scoring_results) -> ScoringResultBundle:
    """M8 最终评分包。"""
    return m8_service.finalize_scoring(scoring_preparation, rubric_scoring_results)


@pytest.fixture
def observation_batch() -> LearningObservationBatch:
    """M5 run_learning_models 用的观测批次。"""
    return LearningObservationBatch(
        batch_id="batch_1",
        learner_id="learner_1",
        observations=[
            LearningObservation(
                observation_id="obs_1",
                learner_id="learner_1",
                course_id="course_1",
                class_id="class_1",
                attempt_id="attempt_1",
                item_id="item_1_1",
                item_version="1.0.0",
                concept_ids=["concept_1"],
                score=5.0,
                max_score=5.0,
                source_audit_id="audit_attempt_1_item_1_instance_1",
                source_audit_version=1,
                occurred_at=FIXED_DT,
            ),
        ],
        watermark="wm_1",
        created_at=FIXED_DT,
    )
