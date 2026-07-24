# 课业智析 M5/M8 完整任务指南

> 负责人：童宇晗
> 模块：M5（学习者与班级状态）+ M8（测评评分）
> 仓库：https://github.com/ZSZH12138/KeYeZhiXi
> 本地路径：`C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo`

---

## 当前状态总览

| 维度 | 状态 |
|------|------|
| M5/M8 服务代码 | ✅ 已完成（service.py + 辅助文件全部写好） |
| 84 个契约类 | ✅ 已定义 |
| AppCoordinator 编排层 | ✅ 已接好 M5/M8 |
| 测试文件 | ❌ **零个**（tests/ 下只有 .gitkeep 空文件） |
| progress/tong.json | ❌ 显示 completed 为空，需要更新 |

**你的核心任务：为 M5/M8 编写完整测试，验证代码正确性，修复发现的 bug。**

---

## 第一步：配置开发环境（5分钟）

### 1.1 安装 pytest

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
python -m pip install pytest
```

### 1.2 验证包可导入

```bash
python -c "from course_insight.modules.m5_learner_class_state.stubs import M5StateServiceStub; print('M5 OK')"
python -c "from course_insight.modules.m8_assessment_scoring.stubs import M8AssessmentServiceStub; print('M8 OK')"
```

如果两个都打印 OK，说明环境就绪。

### 1.3 在 pyproject.toml 中添加 pytest 配置

在 `pyproject.toml` 末尾添加：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
```

---

## 第二步：理解你负责的代码（20分钟）

### 2.1 M5 模块文件结构

```
src/course_insight/modules/m5_learner_class_state/
├── service.py          ← 公开服务入口（2个方法）
├── update_policy.py    ← 诊断 + 学习者状态更新逻辑
├── aggregation.py      ← 班级状态聚合逻辑
├── repository.py       ← 持久化接口（Protocol，当前用空对象）
├── stubs.py            ← 零参数构造器（方便测试用）
└── README.md           ← 模块说明
```

### 2.2 M5 两个公开方法

| 方法 | 功能 | 当前状态 |
|------|------|---------|
| `update_state()` | 从 M8 评分包更新诊断+学习者状态+班级状态 | ✅ 已实现 |
| `run_learning_models()` | 运行 DINA/BKT 模型 | 🔲 空壳（返回 empty，这是设计要求，不需要你实现） |

### 2.3 M8 模块文件结构

```
src/course_insight/modules/m8_assessment_scoring/
├── service.py          ← 公开服务入口（6个方法）
├── paper_generator.py  ← 组卷逻辑（按蓝图选题并冻结）
├── rule_scorer.py      ← 客观题自动评分
├── repository.py       ← 持久化接口（Protocol）
├── stubs.py            ← 零参数构造器
└── README.md           ← 模块说明
```

### 2.4 M8 六个公开方法

| 方法 | 功能 | 当前状态 |
|------|------|---------|
| `generate_paper()` | 按 M4 任务+M3 知识包生成冻结试卷 | ✅ 已实现 |
| `prepare_scoring()` | 解析学生答案，拆分客观/主观评分路径 | ✅ 已实现 |
| `finalize_scoring()` | 合并客观+主观评分为审计包 | ✅ 已实现 |
| `apply_teacher_review()` | 应用教师复核覆盖 | ✅ 已实现 |
| `calibrate_irt()` | IRT 标定 | 🔲 空壳（设计要求，不需要实现） |
| `select_adaptive_items()` | 自适应选题 | 🔲 空壳（设计要求，不需要实现） |

### 2.5 关键规则

- 🔲 空壳方法（`run_learning_models`、`calibrate_irt`、`select_adaptive_items`）**必须保持空壳**，项目禁止伪造模型参数
- 你只需要测试 ✅ 已实现的方法，以及验证 🔲 空壳方法确实返回 empty 状态

---

## 第三步：创建测试 fixtures（15分钟）

测试需要构造模拟数据（KnowledgeBundle、TaskPlan 等）。这些数据较复杂，需要放在 fixture 文件中复用。

### 3.1 创建测试目录结构

```
tests/
├── conftest.py                    ← pytest 全局 fixtures
├── unit/
│   ├── test_m5_update_state.py    ← M5 update_state 测试
│   ├── test_m5_learning_models.py ← M5 run_learning_models 测试
│   ├── test_m5_update_policy.py   ← M5 诊断策略测试
│   ├── test_m5_aggregation.py     ← M5 班级聚合测试
│   ├── test_m8_generate_paper.py  ← M8 组卷测试
│   ├── test_m8_prepare_scoring.py ← M8 评分准备测试
│   ├── test_m8_finalize_scoring.py← M8 评分合并测试
│   ├── test_m8_teacher_review.py  ← M8 教师复核测试
│   ├── test_m8_irt_stubs.py       ← M8 IRT 空壳测试
│   ├── test_m8_rule_scorer.py     ← M8 客观评分测试
│   └── test_m8_paper_generator.py ← M8 组卷器测试
├── contract/
│   └── test_m5_m8_contracts.py    ← 契约序列化往返测试
└── integration/
    └── test_m8_m5_chain.py        ← M8→M5 串联测试
```

### 3.2 创建 conftest.py

在 `tests/conftest.py` 中定义以下 fixtures：

1. **`state_policy_path`** — 创建临时 JSON 文件，写入 StatePolicy 所需字段
2. **`knowledge_bundle`** — 构造一个最小可用的 KnowledgeBundle（含 concepts、items、rubrics、blueprints、q_matrix）
3. **`task_plan`** — 构造一个要求测评的 TaskPlan
4. **`raw_answer_path`** — 创建临时 JSON 文件，写入学生答案
5. **`m5_service`** — 返回 `M5StateServiceStub()` 实例
6. **`m8_service`** — 返回 `M8AssessmentServiceStub()` 实例

### 3.3 构造 KnowledgeBundle 的要点

KnowledgeBundle 需要包含：
- 至少 1 个 `KnowledgeConcept`
- 至少 2 个 `ItemCard`（1 个客观题 + 1 个主观题）
- 至少 1 个 `Rubric`（主观题用，含 `RubricCriterion` 和 `ReviewPolicy`）
- 至少 1 个 `AssessmentBlueprint`（含 `BlueprintSection`）
- 至少 1 个 `QMatrixEntry`
- `MisconceptionTag` 可选但建议包含
- `PrerequisiteRelation` 可选但建议包含

---

## 第四步：编写 M5 单元测试（30分钟）

### 4.1 test_m5_update_state.py

需要测试的场景：

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_update_state_success` | 正常更新状态 | 返回 StateUpdateResult，包含诊断+学习者状态+班级状态 |
| `test_update_state_no_audits` | 评分包无审计记录 | 抛出 INSUFFICIENT_EVIDENCE |
| `test_update_state_stale_version` | 重复处理同一审计版本 | 抛出 STALE_STATE_VERSION |
| `test_update_state_with_previous` | 带前版快照更新 | state_version 递增 |
| `test_update_state_assert_consistent` | 返回结果的 assert_consistent | 不抛异常 |

### 4.2 test_m5_learning_models.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_run_learning_models_empty` | 正常调用 | 返回 status="empty" 的 LearningModelRun |
| `test_run_learning_models_preserves_learner` | 传入 observation_batch | learner_id 一致 |
| `test_run_learning_models_preserves_count` | 传入 N 条观测 | observation_count == N |

### 4.3 test_m5_update_policy.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_state_policy_from_path` | 从 JSON 加载策略 | 字段正确 |
| `test_state_policy_invalid` | 缺少字段或阈值越界 | 抛出 STATE_POLICY_INVALID |
| `test_build_diagnosis` | 构建诊断 | DiagnosisResult 字段正确 |
| `test_build_learner_state` | 构建学习者状态 | LearnerStateSnapshot 字段正确 |
| `test_latest_audits_dedup` | 同一 audit_id 多版本 | 只保留最新版本 |

### 4.4 test_m5_aggregation.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_aggregate_single_learner` | 单学习者聚合 | ClassStateSnapshot 字段正确 |
| `test_aggregate_with_previous` | 带前版聚合 | mastery_trend_delta 非空 |
| `test_aggregate_coverage` | 覆盖率计算 | coverage_rate == 1/class_size |

---

## 第五步：编写 M8 单元测试（40分钟）

### 5.1 test_m8_generate_paper.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_generate_paper_success` | 正常组卷 | AssessmentPaper 冻结，checksum 匹配 |
| `test_generate_paper_total_score` | 试卷总分 | 等于蓝图总分 |
| `test_generate_paper_no_blueprint` | TaskPlan 无 blueprint_id | 抛出 BLUEPRINT_UNSATISFIABLE |
| `test_generate_paper_blueprint_not_approved` | 蓝图未审批 | 抛出 BLUEPRINT_UNSATISFIABLE |
| `test_generate_paper_items_match_section` | 每个section题目数 | 等于 BlueprintSection.item_count |

### 5.2 test_m8_prepare_scoring.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_prepare_scoring_success` | 正常准备评分 | ScoringPreparationResult 字段正确 |
| `test_prepare_scoring_objective` | 客观题 | 在 objective_audit_records 中 |
| `test_prepare_scoring_subjective` | 主观题 | 在 rubric_scoring_tasks 中 |
| `test_prepare_scoring_checksum_invalid` | 试卷 checksum 不匹配 | 抛出 ANSWER_FORMAT_INVALID |
| `test_prepare_scoring_missing_item` | 答案缺少题目 | 抛出 ANSWER_FORMAT_INVALID |
| `test_prepare_scoring_extra_item` | 答案多出题目 | 抛出 ANSWER_FORMAT_INVALID |
| `test_prepare_scoring_wrong_json` | JSON 格式错误 | 抛出 ANSWER_FORMAT_INVALID |

### 5.3 test_m8_finalize_scoring.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_finalize_scoring_success` | 正常合并评分 | ScoringResultBundle 字段正确 |
| `test_finalize_scoring_total` | 总分计算 | total_score == 各审计记录之和 |
| `test_finalize_scoring_remediation` | 补救计划 | targets 包含缺失概念 |
| `test_finalize_scoring_mismatch` | 量规结果与任务不匹配 | 抛出 SCORING_TASK_RESULT_MISMATCH |
| `test_finalize_scoring_rubric_invalid` | 量规评分越界 | 抛出 RUBRIC_RESULT_INVALID |

### 5.4 test_m8_teacher_review.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_teacher_review_approve` | 教师确认 | audit_version 递增，status="approved" |
| `test_teacher_review_override` | 教师覆盖分数 | 分数被替换，method="teacher_override" |
| `test_teacher_review_reject` | 教师拒绝 | status="rejected" |
| `test_teacher_review_version_conflict` | 版本不匹配 | 抛出 REVIEW_VERSION_CONFLICT |
| `test_teacher_review_total_mismatch` | 确认但总分变了 | 抛出 REVIEW_TOTAL_MISMATCH |

### 5.5 test_m8_irt_stubs.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_calibrate_irt_empty` | 调用 IRT 标定 | status="empty"，converged=False |
| `test_select_adaptive_empty` | 调用自适应选题 | status="empty"，item_ids=[] |

### 5.6 test_m8_rule_scorer.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_rule_scorer_correct` | 答案正确 | total_score == max_score |
| `test_rule_scorer_wrong` | 答案错误 | total_score == 0 |
| `test_rule_scorer_normalize` | 答案大小写/空格 | 正确归一化后比较 |

### 5.7 test_m8_paper_generator.py

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_paper_generator_freeze` | 冻结试卷 | immutable_checksum 匹配 |
| `test_paper_generator_section_score` | 每section分数 | 等于 BlueprintSection.score |
| `test_paper_generator_no_approved_items` | 无审批题目 | 抛出 BLUEPRINT_UNSATISFIABLE |

---

## 第六步：编写契约测试（15分钟）

### 6.1 test_m5_m8_contracts.py

对 M5/M8 相关的根契约做序列化往返测试：

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_state_update_result_roundtrip` | StateUpdateResult → dict → StateUpdateResult | 字段一致 |
| `test_learner_state_snapshot_roundtrip` | LearnerStateSnapshot 往返 | 字段一致 |
| `test_class_state_snapshot_roundtrip` | ClassStateSnapshot 往返 | 字段一致 |
| `test_diagnosis_result_roundtrip` | DiagnosisResult 往返 | 字段一致 |
| `test_assessment_paper_roundtrip` | AssessmentPaper 往返 | 字段一致 |
| `test_scoring_result_bundle_roundtrip` | ScoringResultBundle 往返 | 字段一致 |
| `test_scoring_preparation_roundtrip` | ScoringPreparationResult 往返 | 字段一致 |

---

## 第七步：编写集成测试（15分钟）

### 7.1 test_m8_m5_chain.py

测试 M8 → M5 的完整链路：

| 测试名 | 场景 | 预期结果 |
|--------|------|---------|
| `test_m8_generate_to_m5_update` | M8 组卷→评分→M5 更新状态 | StateUpdateResult 正确 |
| `test_m8_review_to_m5_recompute` | M8 教师复核→M5 重算状态 | 新版本 state_version 递增 |

---

## 第八步：运行全部测试并修复 bug（20分钟）

### 8.1 运行测试

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
python -m pytest tests/ -v --tb=short
```

### 8.2 修复 bug

如果测试发现 bug，在对应的源文件中修复，然后重新运行测试直到全部通过。

### 8.3 检查覆盖率

确保以下路径都被测试覆盖：
- M5 `update_state` 的所有 DomainError 分支
- M8 `prepare_scoring` 的所有校验分支
- M8 `finalize_scoring` 的量规校验分支
- M8 `apply_teacher_review` 的 approve/override/reject 三条路径

---

## 第九步：更新进度文件（2分钟）

编辑 `progress/tong.json`：

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

## 第十步：提交代码（5分钟）

```bash
cd C:\Users\HUAWEI\WorkBuddy\2026-07-17-task-67\KeYeZhiXi_repo
git add tests/ progress/tong.json pyproject.toml
git commit -m "test(M5/M8): add comprehensive unit, contract and integration tests"
git push
```

---

## 总结：你的任务清单

| 步骤 | 内容 | 预计时间 |
|------|------|---------|
| 第一步 | 配置环境（装 pytest） | 5分钟 |
| 第二步 | 读代码理解 M5/M8 | 20分钟 |
| 第三步 | 创建测试 fixtures | 15分钟 |
| 第四步 | M5 单元测试（4个文件） | 30分钟 |
| 第五步 | M8 单元测试（7个文件） | 40分钟 |
| 第六步 | 契约测试（1个文件） | 15分钟 |
| 第七步 | 集成测试（1个文件） | 15分钟 |
| 第八步 | 运行测试修复 bug | 20分钟 |
| 第九步 | 更新进度文件 | 2分钟 |
| 第十步 | 提交代码 | 5分钟 |

**总计约 2.5 小时，完成后你的 M5/M8 任务就完善了。**
