"""M5/M8 测试覆盖补全：17 个测试用例填补骨架代码与参考实现之间的缺口。

M5 (7 个):
  1. STALE_STATE_VERSION — previous course_id 不匹配
  2. 禁止混用学习者 — previous learner_id 不匹配
  3. 空观测列表 watermark 必须明确
  4. watermark 必须保留
  5. LearningModelRun 原子绑定
  6. STATE_REFERENCE_MISMATCH — remediation 引用不存在的 concept
  7. StatePolicy 校验 — class_size<1, minimum_assessed_count<1

M8 (10 个):
  1. AssessmentSubmission 输入
  2. generate_paper 带 M5 状态/诊断输入
  3. 禁止越界分数 — score > max_score
  4. 禁止无证据正分 — score>0 但 course_evidence_id 为 None
  5. RUBRIC_RESULT_INVALID — criterion_ids/confidence/total
  6. REVIEW_TOTAL_MISMATCH — confirm 决定 final_total_score 不一致
  7. prepare_scoring 身份不匹配 — paper_id/learner_id
  8. RuleScorer 非客观题
  9. RuleScorer 缺 answer_key "answer" 字段
  10. finalize_scoring 重复 scoring_task_id
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    ItemInstance,
    PaperSection,
    RemediationPlan,
    RemediationTarget,
    RubricScoringResult,
    RubricScoringTask,
    ScoreAuditRecord,
    ScoringPreparationResult,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    MisconceptionTag,
    PrerequisiteRelation,
    QMatrixEntry,
    ReviewPolicy,
    Rubric,
    RubricCriterion,
)
from course_insight.contracts.learning_models import (
    CognitiveDiagnosisResult,
    KnowledgeTraceSnapshot,
    LearningModelRun,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.contracts.platform import AssessmentSubmission
from course_insight.contracts.state import (
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
)
from course_insight.contracts.analytics import (
    CriterionOverride,
    TeacherReviewDecision,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m5_learner_class_state.stubs import M5StateServiceStub
from course_insight.modules.m5_learner_class_state.update_policy import StatePolicy
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.stubs import M8AssessmentServiceStub


# ────────────────────── 时间常量 ──────────────────────

FIXED_DT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


# ────────────────────── 辅助构建函数 ──────────────────────


def _build_knowledge_bundle() -> KnowledgeBundle:
    """构建含 2 知识点、1 误区、1 量规、1 客观题、1 主观题、1 蓝图的知识包。"""
    concept_1 = KnowledgeConcept(
        concept_id="concept_1",
        name="线性方程",
        chapter_id="chapter_1",
        description="一元一次方程的解法",
        aliases=[],
        status="published",
    )
    concept_2 = KnowledgeConcept(
        concept_id="concept_2",
        name="不等式",
        chapter_id="chapter_1",
        description="一元一次不等式",
        aliases=[],
        status="published",
    )
    prereq = PrerequisiteRelation(
        from_concept_id="concept_1",
        to_concept_id="concept_2",
        relation_type="prerequisite",
        strength=0.8,
    )
    misconception = MisconceptionTag(
        misconception_id="misconception_1",
        name="符号错误",
        description="移项时忘记变号",
        concept_ids=["concept_1"],
        evidence_rules=["rule_1"],
    )
    rubric = Rubric(
        rubric_id="rubric_1",
        version="1.0.0",
        total_score=5.0,
        criteria=[
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
        ],
        review_policy=ReviewPolicy(
            low_confidence_threshold=0.5,
            double_score_disagreement_threshold=0.3,
            require_evidence_for_positive_score=True,
        ),
        status="teacher_approved",
    )
    objective_item = ItemCard(
        item_id="item_obj_1",
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
    subjective_item = ItemCard(
        item_id="item_sub_1",
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
    blueprint = AssessmentBlueprint(
        blueprint_id="blueprint_1",
        version="1.0.0",
        course_id="course_1",
        sections=[
            BlueprintSection(
                section_id="section_1",
                name="基础测试",
                item_count=2,
                score=10.0,
                item_types=[],
                concept_weights={},
                difficulty_range=(0, 5),
                anchor_item_ids=[],
            )
        ],
        total_score=10.0,
        duration_minutes=60,
        status="teacher_approved",
    )
    q_matrix = [
        QMatrixEntry(
            item_id="item_obj_1",
            item_version="1.0.0",
            concept_id="concept_1",
            weight=1.0,
        ),
        QMatrixEntry(
            item_id="item_sub_1",
            item_version="1.0.0",
            concept_id="concept_2",
            weight=1.0,
        ),
    ]
    return KnowledgeBundle(
        knowledge_bundle_id="kb_1",
        course_package_id="cp_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[concept_1, concept_2],
        prerequisite_relations=[prereq],
        misconception_tags=[misconception],
        items=[objective_item, subjective_item],
        rubrics=[rubric],
        blueprints=[blueprint],
        q_matrix=q_matrix,
        status="published",
        published_at=FIXED_DT,
    )


def _build_task_plan() -> TaskPlan:
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


def _build_concept_state(
    concept_id: str = "concept_1",
    mastery: float = 0.5,
) -> ConceptState:
    """构建最小有效的 ConceptState。"""
    return ConceptState(
        concept_id=concept_id,
        mastery_probability=mastery,
        mastery_confidence=0.5,
        misconceptions=[],
        hint_dependency=0.0,
        recent_correction_rate=0.0,
        evidence_count=0,
        updated_at=FIXED_DT,
    )


def _build_learner_state_snapshot(
    *,
    course_id: str = "course_1",
    learner_id: str = "learner_1",
    state_version: int = 1,
) -> LearnerStateSnapshot:
    """构建最小有效的 LearnerStateSnapshot。"""
    return LearnerStateSnapshot(
        snapshot_id="ls_prev_1",
        course_id=course_id,
        class_id="class_1",
        learner_id=learner_id,
        state_version=state_version,
        concept_states=[_build_concept_state()],
        overall_mastery=0.5,
        evidence_count=0,
        updated_at=FIXED_DT,
    )


def _write_state_policy(tmp_path: Path, **overrides: Any) -> Path:
    """写入 StatePolicy 兼容的 JSON 文件，返回路径。"""
    data: dict[str, Any] = {
        "aggregation_policy_version": "1.0.0",
        "class_id": "class_1",
        "class_size": 1,
        "consolidating_threshold": 0.4,
        "mastered_threshold": 0.8,
        "minimum_assessed_count": 1,
        "minimum_coverage": 0.0,
        "misconception_activation_threshold": 0.5,
    }
    data.update(overrides)
    path = tmp_path / "state_policy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_raw_answers(
    tmp_path: Path,
    paper: AssessmentPaper,
    *,
    paper_id: str | None = None,
    learner_id: str | None = None,
    objective_answer: str = "5",
    subjective_answer: str = "设x为未知数，3x=15，x=5",
) -> Path:
    """写入原始答案 JSON 文件。"""
    items = paper.all_items()
    data: dict[str, Any] = {
        "attempt_id": "attempt_1",
        "paper_id": paper_id or paper.paper_id,
        "learner_id": learner_id or paper.learner_id,
        "answers": [],
    }
    for item in items:
        if item.is_subjective():
            data["answers"].append(
                {"item_instance_id": item.item_instance_id, "answer": subjective_answer}
            )
        else:
            data["answers"].append(
                {"item_instance_id": item.item_instance_id, "answer": objective_answer}
            )
    path = tmp_path / "raw_answers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _build_paper_and_prep(
    m8: M8AssessmentServiceStub,
    bundle: KnowledgeBundle,
    task_plan: TaskPlan,
    tmp_path: Path,
) -> tuple[AssessmentPaper, ScoringPreparationResult]:
    """生成试卷并准备评分，返回 (paper, prep)。"""
    paper = m8.generate_paper(task_plan, bundle, None, None)
    raw_path = _write_raw_answers(tmp_path, paper)
    prep = m8.prepare_scoring(paper, raw_path, bundle)
    return paper, prep


def _build_valid_rubric_result(prep: ScoringPreparationResult) -> RubricScoringResult:
    """构建与 prep 中主观题任务匹配的有效 RubricScoringResult。"""
    task = prep.rubric_scoring_tasks[0]
    return RubricScoringResult(
        scoring_task_id=task.scoring_task_id,
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
        scored_at=FIXED_DT,
    )


def _build_scoring_bundle(
    m8: M8AssessmentServiceStub,
    bundle: KnowledgeBundle,
    task_plan: TaskPlan,
    tmp_path: Path,
) -> tuple[ScoringResultBundle, AssessmentPaper, ScoringPreparationResult]:
    """生成完整的评分结果包。"""
    paper, prep = _build_paper_and_prep(m8, bundle, task_plan, tmp_path)
    rubric_results = [_build_valid_rubric_result(prep)]
    scoring_bundle = m8.finalize_scoring(prep, rubric_results)
    return scoring_bundle, paper, prep


# ────────────────────── M5 测试 (7 个) ──────────────────────


class TestM5Coverage:
    """M5 学习者/班级状态模块测试覆盖补全。"""

    def test_m5_1_stale_state_version_course_mismatch(self, tmp_path: Path) -> None:
        """M5-1: previous 的 course_id 不匹配时抛出 STALE_STATE_VERSION。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        scoring_bundle, _, _ = _build_scoring_bundle(m8, bundle, task_plan, tmp_path)
        policy_path = _write_state_policy(tmp_path)

        previous = _build_learner_state_snapshot(course_id="WRONG_COURSE")
        m5 = M5StateServiceStub()
        with pytest.raises(DomainError) as exc:
            m5.update_state(scoring_bundle, bundle, previous, None, policy_path)
        assert exc.value.code == "STALE_STATE_VERSION"

    def test_m5_2_stale_state_version_learner_mismatch(self, tmp_path: Path) -> None:
        """M5-2: previous.learner_id 与 bundle.learner_id 不匹配时抛出 STALE_STATE_VERSION。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        scoring_bundle, _, _ = _build_scoring_bundle(m8, bundle, task_plan, tmp_path)
        policy_path = _write_state_policy(tmp_path)

        previous = _build_learner_state_snapshot(learner_id="WRONG_LEARNER")
        m5 = M5StateServiceStub()
        with pytest.raises(DomainError) as exc:
            m5.update_state(scoring_bundle, bundle, previous, None, policy_path)
        assert exc.value.code == "STALE_STATE_VERSION"

    def test_m5_3_empty_observation_batch_watermark(self) -> None:
        """M5-3: 空观测列表的 batch 仍须有 watermark，run_learning_models 返回 status='empty'。"""
        empty_batch = LearningObservationBatch(
            batch_id="batch_empty",
            learner_id="learner_1",
            observations=[],
            watermark="wm_empty_001",
            created_at=FIXED_DT,
        )
        m5 = M5StateServiceStub()
        result = m5.run_learning_models(empty_batch)
        assert isinstance(result, LearningModelRun)
        assert result.status == "empty"
        assert result.observation_count == 0
        assert result.diagnosis.status == "empty"
        assert result.knowledge_trace.status == "empty"

    def test_m5_4_watermark_preserved(self) -> None:
        """M5-4: KnowledgeTraceSnapshot.observation_watermark 必须与输入批次一致。"""
        batch = LearningObservationBatch(
            batch_id="batch_1",
            learner_id="learner_1",
            observations=[
                LearningObservation(
                    observation_id="obs_1",
                    learner_id="learner_1",
                    course_id="course_1",
                    class_id="class_1",
                    attempt_id="attempt_1",
                    item_id="item_obj_1",
                    item_version="1.0.0",
                    concept_ids=["concept_1"],
                    score=5.0,
                    max_score=5.0,
                    source_audit_id="audit_1",
                    source_audit_version=1,
                    occurred_at=FIXED_DT,
                ),
            ],
            watermark="wm_unique_42",
            created_at=FIXED_DT,
        )
        m5 = M5StateServiceStub()
        result = m5.run_learning_models(batch)
        assert result.knowledge_trace.observation_watermark == "wm_unique_42"

    def test_m5_5_learning_model_run_atomic_binding(self) -> None:
        """M5-5: LearningModelRun 的 diagnosis+knowledge_trace+count+status 必须原子一致。"""
        batch = LearningObservationBatch(
            batch_id="batch_atomic",
            learner_id="learner_1",
            observations=[
                LearningObservation(
                    observation_id="obs_1",
                    learner_id="learner_1",
                    course_id="course_1",
                    class_id="class_1",
                    attempt_id="attempt_1",
                    item_id="item_obj_1",
                    item_version="1.0.0",
                    concept_ids=["concept_1"],
                    score=5.0,
                    max_score=5.0,
                    source_audit_id="audit_1",
                    source_audit_version=1,
                    occurred_at=FIXED_DT,
                ),
                LearningObservation(
                    observation_id="obs_2",
                    learner_id="learner_1",
                    course_id="course_1",
                    class_id="class_1",
                    attempt_id="attempt_1",
                    item_id="item_sub_1",
                    item_version="1.0.0",
                    concept_ids=["concept_2"],
                    score=3.0,
                    max_score=5.0,
                    source_audit_id="audit_2",
                    source_audit_version=1,
                    occurred_at=FIXED_DT,
                ),
            ],
            watermark="wm_atomic",
            created_at=FIXED_DT,
        )
        m5 = M5StateServiceStub()
        result = m5.run_learning_models(batch)
        # 原子绑定：count 一致
        assert result.observation_count == 2
        assert result.diagnosis.observation_count == 2
        assert result.knowledge_trace.observation_count == 2
        # 原子绑定：learner_id 一致
        assert result.diagnosis.learner_id == result.knowledge_trace.learner_id
        # 原子绑定：status 一致
        assert result.status == "empty"
        assert result.diagnosis.status == "empty"
        assert result.knowledge_trace.status == "empty"
        # 原子绑定：empty 状态不携带概率
        assert result.diagnosis.concept_mastery == {}
        assert result.knowledge_trace.concept_probabilities == {}

    def test_m5_6_state_reference_mismatch(self, tmp_path: Path) -> None:
        """M5-6: remediation targets 引用不存在的 concept 时抛出 STATE_REFERENCE_MISMATCH。"""
        bundle = _build_knowledge_bundle()
        policy_path = _write_state_policy(tmp_path)

        # 构造引用不存在 concept 的补救计划
        bad_remediation = RemediationPlan(
            plan_id="plan_bad",
            based_on_attempt_id="attempt_1",
            learner_id="learner_1",
            targets=[
                RemediationTarget(
                    concept_id="NONEXISTENT_CONCEPT",
                    misconception_id=None,
                    priority=1,
                    recommended_item_ids=[],
                    reason="引用不存在的知识点",
                )
            ],
            created_at=FIXED_DT,
        )
        # 构造最小评分包
        audit = ScoreAuditRecord(
            audit_id="audit_1",
            audit_version=1,
            attempt_id="attempt_1",
            item_instance_id="inst_1",
            criterion_scores=[
                CriterionScore(
                    criterion_id="c1",
                    score=3.0,
                    student_evidence="证据",
                    course_evidence_id=None,
                    reason="测试",
                ),
            ],
            total_score=3.0,
            max_score=5.0,
            confidence=1.0,
            scoring_method="rule",
            review_status="not_required",
            review_reason=[],
            created_at=FIXED_DT,
        )
        scoring_bundle = ScoringResultBundle(
            attempt_id="attempt_1",
            paper_id="paper_1",
            learner_id="learner_1",
            score_audit_records=[audit],
            learning_events=[],
            remediation_plan=bad_remediation,
            total_score=3.0,
            max_score=5.0,
            finalized_at=FIXED_DT,
        )
        m5 = M5StateServiceStub()
        with pytest.raises(DomainError) as exc:
            m5.update_state(scoring_bundle, bundle, None, None, policy_path)
        assert exc.value.code == "STATE_REFERENCE_MISMATCH"

    def test_m5_7_state_policy_validation(self, tmp_path: Path) -> None:
        """M5-7: StatePolicy.from_path 校验 class_size<1 和 minimum_assessed_count<1。"""
        # class_size < 1
        path_1 = tmp_path / "policy_bad_class_size.json"
        path_1.write_text(
            json.dumps({
                "aggregation_policy_version": "1.0.0",
                "class_id": "class_1",
                "class_size": 0,
                "consolidating_threshold": 0.4,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 1,
                "minimum_coverage": 0.0,
                "misconception_activation_threshold": 0.5,
            }),
            encoding="utf-8",
        )
        with pytest.raises(DomainError) as exc:
            StatePolicy.from_path(path_1)
        assert exc.value.code == "STATE_POLICY_INVALID"

        # minimum_assessed_count < 1
        path_2 = tmp_path / "policy_bad_min_assessed.json"
        path_2.write_text(
            json.dumps({
                "aggregation_policy_version": "1.0.0",
                "class_id": "class_1",
                "class_size": 1,
                "consolidating_threshold": 0.4,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 0,
                "minimum_coverage": 0.0,
                "misconception_activation_threshold": 0.5,
            }),
            encoding="utf-8",
        )
        with pytest.raises(DomainError) as exc:
            StatePolicy.from_path(path_2)
        assert exc.value.code == "STATE_POLICY_INVALID"

        # 正常策略应通过
        valid_path = _write_state_policy(tmp_path)
        policy = StatePolicy.from_path(valid_path)
        assert policy.class_size == 1
        assert policy.mastered_threshold == 0.8


# ────────────────────── M8 测试 (10 个) ──────────────────────


class TestM8Coverage:
    """M8 测评评分模块测试覆盖补全。"""

    def test_m8_1_prepare_scoring_accepts_submission(self, tmp_path: Path) -> None:
        """M8-1: prepare_scoring 接受 AssessmentSubmission 对象作为输入。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        paper = m8.generate_paper(task_plan, bundle, None, None)

        items = paper.all_items()
        answers: dict[str, str] = {}
        for item in items:
            if item.is_subjective():
                answers[item.item_instance_id] = "设x为未知数，3x=15，x=5"
            else:
                answers[item.item_instance_id] = "5"

        submission = AssessmentSubmission(
            submission_id="sub_1",
            attempt_id="attempt_1",
            paper_id=paper.paper_id,
            learner_id=paper.learner_id,
            answers=answers,
            submitted_at=FIXED_DT,
        )
        prep = m8.prepare_scoring(paper, submission, bundle)
        assert isinstance(prep, ScoringPreparationResult)
        assert prep.attempt_id == "attempt_1"
        assert prep.paper_id == paper.paper_id
        assert prep.learner_id == paper.learner_id

    def test_m8_2_generate_paper_with_m5_inputs(self, tmp_path: Path) -> None:
        """M8-2: generate_paper 接受 M5 状态/诊断输入并正常生成试卷。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()

        learner_state = LearnerStateSnapshot(
            snapshot_id="ls_1",
            course_id="course_1",
            class_id="class_1",
            learner_id="learner_1",
            state_version=1,
            concept_states=[
                _build_concept_state("concept_1", 0.3),
                _build_concept_state("concept_2", 0.8),
            ],
            overall_mastery=0.55,
            evidence_count=1,
            updated_at=FIXED_DT,
        )
        item_diag = ItemDiagnosis(
            item_instance_id="inst_1",
            concept_ids=["concept_1"],
            misconception_ids=[],
            error_type="calculation",
            confidence=0.8,
            evidence_audit_ids=["audit_1"],
            prerequisite_gap_ids=[],
        )
        diagnosis = DiagnosisResult(
            diagnosis_id="diag_1",
            attempt_id="attempt_0",
            learner_id="learner_1",
            item_diagnoses=[item_diag],
            priority_concept_ids=["concept_1"],
            priority_misconception_ids=[],
            generated_at=FIXED_DT,
        )
        paper = m8.generate_paper(task_plan, bundle, learner_state, diagnosis)
        assert isinstance(paper, AssessmentPaper)
        assert len(paper.all_items()) == 2
        assert paper.total_score() == 10.0

    def test_m8_3_rubric_score_exceeds_max(self, tmp_path: Path) -> None:
        """M8-3: 主观题总分超过量规上限时抛出 RUBRIC_RESULT_INVALID。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        paper, prep = _build_paper_and_prep(m8, bundle, task_plan, tmp_path)

        task = prep.rubric_scoring_tasks[0]
        # 总分 6.0 超过量规上限 5.0
        bad_result = RubricScoringResult(
            scoring_task_id=task.scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion_1",
                    score=4.0,
                    student_evidence="证据",
                    course_evidence_id="evidence_1",
                    reason="超额",
                ),
                CriterionScore(
                    criterion_id="criterion_2",
                    score=2.0,
                    student_evidence="证据",
                    course_evidence_id="evidence_2",
                    reason="超额",
                ),
            ],
            total_score=6.0,  # 超过 rubric.total_score=5.0
            confidence=0.9,
            missing_concept_ids=[],
            review_flags=[],
            model_name="local_model",
            model_version="1.0.0",
            scored_at=FIXED_DT,
        )
        with pytest.raises(DomainError) as exc:
            m8.finalize_scoring(prep, [bad_result])
        assert exc.value.code == "RUBRIC_RESULT_INVALID"

    def test_m8_4_positive_score_without_evidence(self, tmp_path: Path) -> None:
        """M8-4: 正分评分点缺少课程证据(course_evidence_id=None)时抛出 RUBRIC_RESULT_INVALID。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        paper, prep = _build_paper_and_prep(m8, bundle, task_plan, tmp_path)

        task = prep.rubric_scoring_tasks[0]
        # criterion_1 有正分但 course_evidence_id=None，
        # 模型层只检查 student_evidence，服务层检查 require_evidence_for_positive_score
        bad_result = RubricScoringResult(
            scoring_task_id=task.scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion_1",
                    score=3.0,
                    student_evidence="学生列出了方程",
                    course_evidence_id=None,  # 缺少课程证据
                    reason="有学生证据但无课程证据",
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
            scored_at=FIXED_DT,
        )
        with pytest.raises(DomainError) as exc:
            m8.finalize_scoring(prep, [bad_result])
        assert exc.value.code == "RUBRIC_RESULT_INVALID"

    def test_m8_5_rubric_result_invalid_various(self, tmp_path: Path) -> None:
        """M8-5: criterion_ids 不匹配、单项超限、课程证据不在量规中均抛出 RUBRIC_RESULT_INVALID。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        paper, prep = _build_paper_and_prep(m8, bundle, task_plan, tmp_path)
        task = prep.rubric_scoring_tasks[0]

        # (a) criterion_ids 不匹配
        bad_ids_result = RubricScoringResult(
            scoring_task_id=task.scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id="WRONG_CRITERION",
                    score=3.0,
                    student_evidence="证据",
                    course_evidence_id=None,
                    reason="错误ID",
                ),
                CriterionScore(
                    criterion_id="criterion_2",
                    score=2.0,
                    student_evidence="证据",
                    course_evidence_id="evidence_2",
                    reason="正确",
                ),
            ],
            total_score=5.0,
            confidence=0.9,
            missing_concept_ids=[],
            review_flags=[],
            model_name="local_model",
            model_version="1.0.0",
            scored_at=FIXED_DT,
        )
        with pytest.raises(DomainError) as exc:
            m8.finalize_scoring(prep, [bad_ids_result])
        assert exc.value.code == "RUBRIC_RESULT_INVALID"

        # (b) 单项分数超过量规评分点上限 (criterion_1 max=3.0 但 score=4.0)
        bad_criterion_max_result = RubricScoringResult(
            scoring_task_id=task.scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion_1",
                    score=4.0,  # 超过 max_score=3.0
                    student_evidence="证据",
                    course_evidence_id="evidence_1",
                    reason="超限",
                ),
                CriterionScore(
                    criterion_id="criterion_2",
                    score=0.0,
                    student_evidence="",
                    course_evidence_id=None,
                    reason="未作答",
                ),
            ],
            total_score=4.0,  # 不超过 rubric total=5.0
            confidence=0.9,
            missing_concept_ids=[],
            review_flags=[],
            model_name="local_model",
            model_version="1.0.0",
            scored_at=FIXED_DT,
        )
        with pytest.raises(DomainError) as exc:
            m8.finalize_scoring(prep, [bad_criterion_max_result])
        assert exc.value.code == "RUBRIC_RESULT_INVALID"

        # (c) 课程证据不在量规评分点的 course_evidence_ids 中
        bad_evidence_result = RubricScoringResult(
            scoring_task_id=task.scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion_1",
                    score=3.0,
                    student_evidence="证据",
                    course_evidence_id="WRONG_EVIDENCE",  # 不在 criterion_1.course_evidence_ids 中
                    reason="错误证据引用",
                ),
                CriterionScore(
                    criterion_id="criterion_2",
                    score=2.0,
                    student_evidence="证据",
                    course_evidence_id="evidence_2",
                    reason="正确",
                ),
            ],
            total_score=5.0,
            confidence=0.9,
            missing_concept_ids=[],
            review_flags=[],
            model_name="local_model",
            model_version="1.0.0",
            scored_at=FIXED_DT,
        )
        with pytest.raises(DomainError) as exc:
            m8.finalize_scoring(prep, [bad_evidence_result])
        assert exc.value.code == "RUBRIC_RESULT_INVALID"

    def test_m8_6_review_total_mismatch(self, tmp_path: Path) -> None:
        """M8-6: confirm 决定的 final_total_score 与审计记录不一致时抛出 REVIEW_TOTAL_MISMATCH。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        scoring_bundle, _, _ = _build_scoring_bundle(m8, bundle, task_plan, tmp_path)

        audit = scoring_bundle.score_audit_records[0]
        # final_total_score 与 audit.total_score 不一致，但不超过 max_score
        decision = TeacherReviewDecision(
            decision_id="decision_1",
            audit_id=audit.audit_id,
            expected_audit_version=audit.audit_version,
            decision="confirm",
            final_total_score=0.0,  # 与 audit.total_score 不匹配
            criterion_overrides=[],
            teacher_comment="确认",
            reviewer_id="teacher_1",
            reviewed_at=FIXED_DT,
        )
        with pytest.raises(DomainError) as exc:
            m8.apply_teacher_review(scoring_bundle, decision)
        assert exc.value.code == "REVIEW_TOTAL_MISMATCH"

    def test_m8_7_prepare_scoring_identity_mismatch(self, tmp_path: Path) -> None:
        """M8-7: paper_id/learner_id 不匹配时抛出 ANSWER_FORMAT_INVALID。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        paper = m8.generate_paper(task_plan, bundle, None, None)

        # paper_id 不匹配
        wrong_paper_path = _write_raw_answers(
            tmp_path, paper, paper_id="WRONG_PAPER_ID"
        )
        with pytest.raises(DomainError) as exc:
            m8.prepare_scoring(paper, wrong_paper_path, bundle)
        assert exc.value.code == "ANSWER_FORMAT_INVALID"

        # learner_id 不匹配
        wrong_learner_path = _write_raw_answers(
            tmp_path, paper, learner_id="WRONG_LEARNER"
        )
        with pytest.raises(DomainError) as exc:
            m8.prepare_scoring(paper, wrong_learner_path, bundle)
        assert exc.value.code == "ANSWER_FORMAT_INVALID"

    def test_m8_8_rule_scorer_non_objective(self) -> None:
        """M8-8: RuleScorer 对非客观题评分时抛出 ANSWER_FORMAT_INVALID。"""
        bundle = _build_knowledge_bundle()
        subjective_item = bundle.get_item("item_sub_1")
        # 构造主观题实例
        instance = ItemInstance(
            item_instance_id="inst_sub_1",
            item_id=subjective_item.item_id,
            item_version=subjective_item.version,
            stem=subjective_item.stem,
            parameters={},
            concept_ids=["concept_2"],
            rubric_id="rubric_1",
            max_score=5.0,
            source_evidence_ids=["evidence_2"],
        )
        scorer = RuleScorer()
        with pytest.raises(DomainError) as exc:
            scorer.score(
                attempt_id="attempt_1",
                item_instance=instance,
                item=subjective_item,
                raw_answer="答案",
            )
        assert exc.value.code == "ANSWER_FORMAT_INVALID"

    def test_m8_9_rule_scorer_missing_answer_key(self) -> None:
        """M8-9: RuleScorer 对缺少 answer_key "answer" 字段的客观题抛出 ANSWER_FORMAT_INVALID。"""
        # 构造缺少 "answer" 字段的客观题
        bad_item = ItemCard(
            item_id="item_no_answer",
            version="1.0.0",
            stem="测试题",
            item_type="multiple_choice",
            concept_ids=["concept_1"],
            misconception_ids=[],
            difficulty_level=2,
            cognitive_level="apply",
            parameter_rules=[],
            answer_key={"max_score": 5.0},  # 缺少 "answer" 字段
            rubric_id=None,
            source_evidence_ids=[],
            status="teacher_approved",
        )
        instance = ItemInstance(
            item_instance_id="inst_no_answer",
            item_id=bad_item.item_id,
            item_version=bad_item.version,
            stem=bad_item.stem,
            parameters={},
            concept_ids=["concept_1"],
            rubric_id=None,
            max_score=5.0,
            source_evidence_ids=[],
        )
        scorer = RuleScorer()
        with pytest.raises(DomainError) as exc:
            scorer.score(
                attempt_id="attempt_1",
                item_instance=instance,
                item=bad_item,
                raw_answer="B",
            )
        assert exc.value.code == "ANSWER_FORMAT_INVALID"

    def test_m8_10_finalize_scoring_duplicate_task_id(self, tmp_path: Path) -> None:
        """M8-10: finalize_scoring 收到重复 scoring_task_id 时抛出 SCORING_TASK_RESULT_MISMATCH。"""
        bundle = _build_knowledge_bundle()
        m8 = M8AssessmentServiceStub()
        task_plan = _build_task_plan()
        paper, prep = _build_paper_and_prep(m8, bundle, task_plan, tmp_path)

        valid_result = _build_valid_rubric_result(prep)
        # 重复同一 scoring_task_id
        duplicate_results = [valid_result, valid_result.model_copy(deep=True)]
        with pytest.raises(DomainError) as exc:
            m8.finalize_scoring(prep, duplicate_results)
        assert exc.value.code == "SCORING_TASK_RESULT_MISMATCH"
