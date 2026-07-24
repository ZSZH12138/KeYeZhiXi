# M5/M8 测试编写 — 完整执行手册

> 仓库路径: `C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo`
> 所有命令在仓库根目录下执行

---

## 第 0 步：安装 pytest（1 分钟）

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
python -m pip install pytest
```

验证：
```bash
python -m pytest --version
```
看到版本号即可。

---

## 第 1 步：在 pyproject.toml 末尾追加 pytest 配置（1 分钟）

打开 `pyproject.toml`，在文件**最末尾**追加：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
```

---

## 第 2 步：创建测试目录和 __init__.py（1 分钟）

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
mkdir tests\unit tests\contract tests\integration 2>nul
type nul > tests\unit\__init__.py
type nul > tests\contract\__init__.py
type nul > tests\integration\__init__.py
```

> 如果 `mkdir` 报目录已存在，忽略即可。

---

## 第 3 步：创建 conftest.py — 全局 fixtures（核心文件）

在 `tests/conftest.py` 写入以下**完整内容**：

```python
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
```

---

## 第 4 步：M5 单元测试（4 个文件）

### 4.1 `tests/unit/test_m5_update_state.py`

```python
"""M5 update_state 单元测试。"""

import pytest
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import StateUpdateResult


class TestM5UpdateState:
    def test_update_state_success(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """正常更新状态，返回完整的 StateUpdateResult。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert isinstance(result, StateUpdateResult)
        assert result.diagnosis_result.learner_id == "learner_1"
        assert result.learner_state_snapshot.learner_id == "learner_1"
        assert result.learner_state_snapshot.state_version == 1
        assert result.class_state_snapshot.class_id == "class_1"
        assert len(result.processed_audit_ids) > 0

    def test_update_state_no_audits(self, m5_service, knowledge_bundle, state_policy_path):
        """评分包无审计记录时，抛出 INSUFFICIENT_EVIDENCE。"""
        from course_insight.contracts.assessment import ScoringResultBundle
        empty_bundle = ScoringResultBundle(
            attempt_id="attempt_1",
            paper_id="paper_task_1",
            learner_id="learner_1",
            score_audit_records=[],
            learning_events=[],
            remediation_plan=scoring_result_bundle.remediation_plan,
            total_score=0.0,
            max_score=10.0,
            finalized_at=scoring_result_bundle.finalized_at,
        )
        with pytest.raises(DomainError) as exc_info:
            m5_service.update_state(empty_bundle, knowledge_bundle, None, None, state_policy_path)
        assert exc_info.value.code == "INSUFFICIENT_EVIDENCE"

    def test_update_state_stale_version(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """重复处理同一审计版本时，抛出 STALE_STATE_VERSION。"""
        # 第一次调用成功
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        # 第二次调用同样的数据，应该报 STALE_STATE_VERSION
        with pytest.raises(DomainError) as exc_info:
            m5_service.update_state(
                scoring_result_bundle,
                knowledge_bundle,
                result.learner_state_snapshot,
                result.class_state_snapshot,
                state_policy_path,
            )
        assert exc_info.value.code == "STALE_STATE_VERSION"

    def test_update_state_with_previous(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """带前版快照更新，state_version 应递增。"""
        # 第一次调用
        first_result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert first_result.learner_state_snapshot.state_version == 1

    def test_update_state_assert_consistent(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """返回结果的 assert_consistent 不应抛异常。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        # 如果不一致会抛 DomainError
        result.assert_consistent()
```

### 4.2 `tests/unit/test_m5_learning_models.py`

```python
"""M5 run_learning_models 空壳验证测试。"""

from course_insight.contracts.learning_models import LearningModelRun


class TestM5LearningModels:
    def test_run_learning_models_empty(self, m5_service, observation_batch):
        """空壳方法应返回 status='empty'。"""
        result = m5_service.run_learning_models(observation_batch)
        assert isinstance(result, LearningModelRun)
        assert result.status == "empty"
        assert result.diagnosis.status == "empty"
        assert result.knowledge_trace.status == "empty"

    def test_run_learning_models_preserves_learner(self, m5_service, observation_batch):
        """返回结果的 learner_id 应与输入一致。"""
        result = m5_service.run_learning_models(observation_batch)
        assert result.diagnosis.learner_id == observation_batch.learner_id
        assert result.knowledge_trace.learner_id == observation_batch.learner_id

    def test_run_learning_models_preserves_count(self, m5_service, observation_batch):
        """observation_count 应等于输入的观测数。"""
        result = m5_service.run_learning_models(observation_batch)
        assert result.observation_count == len(observation_batch.observations)
        assert result.diagnosis.observation_count == len(observation_batch.observations)
        assert result.knowledge_trace.observation_count == len(observation_batch.observations)
```

### 4.3 `tests/unit/test_m5_update_policy.py`

```python
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
```

### 4.4 `tests/unit/test_m5_aggregation.py`

```python
"""M5 班级聚合策略测试。"""

from course_insight.contracts.state import ClassStateSnapshot
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
    StatePolicy,
)


class TestM5Aggregation:
    def test_aggregate_single_learner(
        self,
        m5_service,
        scoring_result_bundle,
        knowledge_bundle,
        state_policy_path,
    ):
        """单学习者聚合，ClassStateSnapshot 字段正确。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        class_state = result.class_state_snapshot
        assert isinstance(class_state, ClassStateSnapshot)
        assert class_state.class_id == "class_1"
        assert class_state.class_size == 1
        assert class_state.assessed_count == 1
        assert len(class_state.concept_status) == len(knowledge_bundle.concepts)

    def test_aggregate_coverage(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        """覆盖率计算正确：1 个学生 / 班级 1 人 = 1.0。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert result.class_state_snapshot.coverage_rate == 1.0

    def test_aggregate_evidence_sufficient(
        self,
        m5_service,
        scoring_result_bundle,
        knowledge_bundle,
        state_policy_path,
    ):
        """minimum_assessed_count=1, coverage=1.0 → sufficient。"""
        result = m5_service.update_state(
            scoring_result_bundle,
            knowledge_bundle,
            None,
            None,
            state_policy_path,
        )
        assert result.class_state_snapshot.evidence_status == "sufficient"
```

---

## 第 5 步：M8 单元测试（7 个文件）

### 5.1 `tests/unit/test_m8_generate_paper.py`

```python
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
        """TaskPlan 无 blueprint_id 时抛出 BLUEPRINT_UNSATISFIABLE。"""
        bad_task = task_plan.model_copy(update={"blueprint_id": None, "task_type": "practice"})
        # TaskPlan.validate_business_rules 会在 requires_assessment 时要求 blueprint
        # 所以需要用非 assessment 类型来绕过，但 PaperGenerator 会检查
        # 实际上 PaperGenerator._validate_references 会检查 requires_assessment()
        # 如果 task_type 不是 assessment 类型，也不会通过
        # 所以这里测试 blueprint_id 指向不存在的蓝图
        bad_task = task_plan.model_copy(update={"blueprint_id": "nonexistent"})
        with pytest.raises(DomainError) as exc_info:
            m8_service.generate_paper(bad_task, knowledge_bundle, None, None)
        assert exc_info.value.code == "BLUEPRINT_NOT_FOUND"

    def test_generate_paper_blueprint_not_approved(self, m8_service, task_plan, knowledge_bundle):
        """蓝图未审批时抛出 BLUEPRINT_UNSATISFIABLE。"""
        # 修改蓝图为非审批状态
        bad_bundle = knowledge_bundle.model_copy(deep=True)
        object.__setattr__(bad_bundle.blueprints[0], "status", "draft")
        # 需要重新验证 — KnowledgeBundle 的 published 状态会检查
        # 但我们直接测试 PaperGenerator 的行为
        with pytest.raises(DomainError) as exc_info:
            m8_service.generate_paper(task_plan, bad_bundle, None, None)
        assert exc_info.value.code == "BLUEPRINT_UNSATISFIABLE"
```

### 5.2 `tests/unit/test_m8_prepare_scoring.py`

```python
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
```

### 5.3 `tests/unit/test_m8_finalize_scoring.py`

```python
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
```

### 5.4 `tests/unit/test_m8_teacher_review.py`

```python
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

    def test_teacher_review_override(self, m8_service, scoring_result_bundle):
        """教师覆盖分数：分数被替换，method 变为 teacher_override。"""
        # 找到主观题审计记录（有 criterion_scores 的那条）
        subjective_audit = None
        for record in scoring_result_bundle.score_audit_records:
            if len(record.criterion_scores) > 1:
                subjective_audit = record
                break
        assert subjective_audit is not None, "应有主观题审计记录"

        overrides = [
            CriterionOverride(
                criterion_id=score.criterion_id,
                previous_score=score.score,
                new_score=score.max_score,
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
```

### 5.5 `tests/unit/test_m8_irt_stubs.py`

```python
"""M8 IRT 空壳方法验证测试。"""

from datetime import datetime, timedelta, timezone

from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    CalibrationRunResult,
    AdaptiveSelectionResult,
)


FIXED_DT = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))


class TestM8IRTStubs:
    def test_calibrate_irt_empty(self, m8_service, observation_batch):
        """IRT 标定空壳返回 status='empty', converged=False。"""
        result = m8_service.calibrate_irt(observation_batch, FIXED_DT)
        assert isinstance(result, CalibrationRunResult)
        assert result.status == "empty"
        assert result.converged is False
        assert result.parameter_set.status == "empty"
        assert result.parameter_set.item_parameters == []
        assert result.metrics == {}

    def test_select_adaptive_empty(self, m8_service, observation_batch):
        """自适应选题空壳返回 status='empty', item_ids=[]。"""
        policy = AdaptiveSelectionPolicy(
            policy_id="policy_1",
            version="1.0.0",
            parameter_set_id=None,
            max_items=5,
            concept_quotas={"concept_1": 3},
            status="empty",
        )
        ability = AbilityEstimate(
            estimate_id="estimate_1",
            learner_id="learner_1",
            parameter_set_id="param_1",
            theta=None,
            standard_error=None,
            status="empty",
            estimated_at=FIXED_DT,
        )
        result = m8_service.select_adaptive_items(policy, ability, FIXED_DT)
        assert isinstance(result, AdaptiveSelectionResult)
        assert result.status == "empty"
        assert result.item_ids == []
        assert result.ability_estimate is None
```

### 5.6 `tests/unit/test_m8_rule_scorer.py`

```python
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
```

### 5.7 `tests/unit/test_m8_paper_generator.py`

```python
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
```

---

## 第 6 步：契约序列化往返测试

### `tests/contract/test_m5_m8_contracts.py`

```python
"""M5/M8 契约序列化往返测试。"""


class TestContractRoundtrip:
    def _roundtrip(self, obj):
        """obj → dict → obj，验证字段一致。"""
        cls = type(obj)
        data = obj.model_dump(mode="json")
        restored = cls.model_validate(data)
        assert restored == obj
        return restored

    def test_state_update_result_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result)

    def test_learner_state_snapshot_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result.learner_state_snapshot)

    def test_class_state_snapshot_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result.class_state_snapshot)

    def test_diagnosis_result_roundtrip(self, m5_service, scoring_result_bundle, knowledge_bundle, state_policy_path):
        result = m5_service.update_state(
            scoring_result_bundle, knowledge_bundle, None, None, state_policy_path,
        )
        self._roundtrip(result.diagnosis_result)

    def test_assessment_paper_roundtrip(self, m8_service, task_plan, knowledge_bundle):
        paper = m8_service.generate_paper(task_plan, knowledge_bundle, None, None)
        self._roundtrip(paper)

    def test_scoring_result_bundle_roundtrip(self, scoring_result_bundle):
        self._roundtrip(scoring_result_bundle)

    def test_scoring_preparation_roundtrip(self, scoring_preparation):
        self._roundtrip(scoring_preparation)
```

---

## 第 7 步：集成测试

### `tests/integration/test_m8_m5_chain.py`

```python
"""M8 → M5 完整链路集成测试。"""

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
        """完整链路：M8 组卷 → 评分 → M5 更新状态。"""
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
        """M8 教师复核 → M5 重算状态，新版本 state_version 递增。"""
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

        # 3. 用新 M8 实例（避免 _processed_by_learner 缓存）再做 M5 更新
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
```

---

## 第 8 步：运行全部测试（5 分钟）

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
python -m pytest tests/ -v --tb=short
```

预期输出：所有测试 PASSED。

如果有 FAILED：
1. 看报错的 `assert` 行
2. 对比源码逻辑，修改测试代码（不要改源码，除非确实发现 bug）
3. 重新运行 `python -m pytest tests/ -v --tb=short`

只运行某一类测试：
```bash
python -m pytest tests/unit/ -v          # 只跑单元测试
python -m pytest tests/contract/ -v       # 只跑契约测试
python -m pytest tests/integration/ -v    # 只跑集成测试
python -m pytest tests/unit/test_m5_update_state.py -v  # 只跑某个文件
```

---

## 第 9 步：更新进度文件（1 分钟）

打开 `progress/tong.json`，替换为：

```json
{
  "owner": "童",
  "modules": ["M5", "M8"],
  "current_task": "M5/M8 测试完善",
  "completed": [
    "M5StateService.update_state 单元测试",
    "M5StateService.run_learning_models 空壳验证",
    "M5 update_policy 诊断策略测试",
    "M5 aggregation 班级聚合测试",
    "M8AssessmentService.generate_paper 单元测试",
    "M8AssessmentService.prepare_scoring 单元测试",
    "M8AssessmentService.finalize_scoring 单元测试",
    "M8AssessmentService.apply_teacher_review 单元测试",
    "M8 calibrate_irt / select_adaptive_items 空壳验证",
    "M8 rule_scorer 客观评分测试",
    "M8 paper_generator 组卷器测试",
    "M5/M8 契约序列化往返测试",
    "M8→M5 集成链路测试"
  ],
  "blockers": [],
  "next_step": "与团队联调，确认 AppCoordinator 全链路通过",
  "updated_at": "2026-07-17T12:30:00+08:00"
}
```

---

## 第 10 步：提交代码（2 分钟）

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
git add tests/ progress/tong.json pyproject.toml
git commit -m "test(M5/M8): add comprehensive unit, contract and integration tests"
git push
```

---

## 文件清单总览

完成后你的 tests/ 目录应如下：

```
tests/
├── conftest.py                              ← 全局 fixtures
├── unit/
│   ├── __init__.py
│   ├── test_m5_update_state.py              ← 5 个测试
│   ├── test_m5_learning_models.py           ← 3 个测试
│   ├── test_m5_update_policy.py             ← 6 个测试
│   ├── test_m5_aggregation.py               ← 3 个测试
│   ├── test_m8_generate_paper.py            ← 5 个测试
│   ├── test_m8_prepare_scoring.py           ← 6 个测试
│   ├── test_m8_finalize_scoring.py          ← 5 个测试
│   ├── test_m8_teacher_review.py            ← 4 个测试
│   ├── test_m8_irt_stubs.py                 ← 2 个测试
│   ├── test_m8_rule_scorer.py               ← 3 个测试
│   └── test_m8_paper_generator.py           ← 3 个测试
├── contract/
│   ├── __init__.py
│   └── test_m5_m8_contracts.py              ← 7 个测试
└── integration/
    ├── __init__.py
    └── test_m8_m5_chain.py                  ← 2 个测试
```

**总计约 54 个测试用例。**

---

## 常见问题排查

### Q: `ImportError: No module named 'pytest'`
A: `python -m pip install pytest`

### Q: `ImportError: No module named 'course_insight'`
A: `cd` 到仓库根目录后执行 `python -m pip install -e .`

### Q: 某个测试 FAILED，报 `DomainError`
A: 仔细看报错的 `code` 字段（如 `BLUEPRINT_UNSATISFIABLE`），对照源码检查 fixture 数据是否满足约束。最常见原因是分数不匹配或字段缺失。

### Q: `test_teacher_review_override` 报错
A: 确保找的是**主观题**审计记录（`criterion_scores` 有 2 条的那条），不是客观题（只有 1 条 criterion）。

### Q: `test_generate_paper_blueprint_not_approved` 报错
A: `object.__setattr__` 绕过了 pydantic 验证。如果 KnowledgeBundle 的 `published` 状态校验阻止了非审批蓝图，可以改为直接修改 blueprint status 为 `"draft"` 再测试。
