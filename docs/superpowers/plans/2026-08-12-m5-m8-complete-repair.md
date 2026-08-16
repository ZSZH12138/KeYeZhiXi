# M5/M8 Complete Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. The user explicitly requires inline execution; do not dispatch subagents.

**Goal:** 修复已发现的全部 M5/M8 基础缺陷，并在本阶段完成 DINA、BKT、2PL IRT、能力估计、自适应选题及参数审核发布闭环。

**Architecture:** 以最新 `origin/main` 为唯一基线，保留主线现有的原子持久化和恢复机制，不把旧审计分支的 v4 数据库迁移直接合入。先建立可信的“试卷—评分—观测—学生状态”数据链，再实现并验证四个模型功能；所有模型产物都要版本化、可追溯，未经质量审核的 IRT 参数不得参与正式选题。

**Tech Stack:** Python 3.11+、Pydantic 2、NumPy、SciPy、SQLite、PostgreSQL、pytest、pytest-cov。

## Global Constraints

- 所有 Python 开发和测试使用 `D:\software\MyAnaconda` 创建的新隔离 Conda 环境。
- 不调用外部 LLM；DINA、BKT、IRT 和自适应选题均为本地确定性算法。
- 空输入可以返回 `empty`；只要输入满足最小数据要求，就必须返回真实计算结果，不能再用固定值或答对率代理冒充模型。
- 所有试卷、评分审计、状态快照和模型参数均为追加式历史；相同身份、相同版本、不同内容必须报冲突，禁止覆盖。
- SQLite 与 PostgreSQL 的行为必须一致。
- 先写失败测试，再实现最小修复；项目总覆盖率不低于 80%，本次新增或重写的 M5/M8 文件覆盖率不低于 90%。
- 所有时间在生产环境使用真实 UTC 时钟；固定时钟只允许在测试中显式注入。
- 不直接继续堆补丁到 `fix/m5-m8-audit-defects`；从最新主线创建新修复分支，选择性移植有效代码。

---

## File Map

### 新建文件

- `src/course_insight/modules/m5_learner_class_state/dina.py`：DINA 拟合与学生属性掌握推断。
- `src/course_insight/modules/m5_learner_class_state/bkt.py`：BKT 参数拟合与时序掌握概率更新。
- `src/course_insight/modules/m8_assessment_scoring/paper_record.py`：保存试卷、课程/班级范围和冻结量规。
- `src/course_insight/modules/m8_assessment_scoring/observation_builder.py`：从冻结试卷和最终评分生成权威学习观测。
- `src/course_insight/modules/m8_assessment_scoring/irt_2pl.py`：2PL 标定和能力估计。
- `src/course_insight/modules/m8_assessment_scoring/adaptive_selector.py`：基于信息量和内容约束的选题器。
- `src/course_insight/infrastructure/postgresql/migrations/0014_m5_m8_model_runtime.sql`：模型、观测和审核历史表。
- `tests/model_validation/test_dina_recovery.py`：DINA 合成数据恢复测试。
- `tests/model_validation/test_bkt_recovery.py`：BKT 合成序列恢复测试。
- `tests/model_validation/test_irt_2pl_recovery.py`：2PL 参数恢复测试。
- `tests/model_validation/test_adaptive_efficiency.py`：自适应选题效果与约束测试。
- `tests/factories/m5_m8.py`：集中创建试卷、评分、学生状态、模型序列和题目池测试数据。

### 主要修改文件

- `pyproject.toml`：增加 NumPy、SciPy 运行依赖。
- `src/course_insight/contracts/assessment.py`：冻结量规版本，校验试卷和评分身份。
- `src/course_insight/contracts/learning_models.py`：补齐模型版本、训练结果、题目池和审核状态。
- `src/course_insight/contracts/state.py`：记录状态所依据的模型运行版本。
- `src/course_insight/application/coordinator.py`：连接 M8 评分、观测构造、M5 模型和状态更新。
- `src/course_insight/modules/m5_learner_class_state/{service,repository,aggregation,update_policy}.py`。
- `src/course_insight/modules/m8_assessment_scoring/{service,repository,paper_generator,recovery,stubs}.py`。
- `src/course_insight/infrastructure/sqlite/{migrations,m5_repository,m8_repository}.py`。
- `src/course_insight/infrastructure/postgresql/{m5_repository,m8_repository}.py`。
- `contracts/schemas/*.schema.json`：重新生成受影响契约 Schema。

---

### Task 1: 建立可合并的主线基线

**Files:**
- Review: `pyproject.toml`
- Review: `src/course_insight/infrastructure/sqlite/migrations.py`
- Review: `src/course_insight/infrastructure/sqlite/m5_repository.py`
- Review: `src/course_insight/infrastructure/sqlite/m8_repository.py`
- Test: `tests/`

**Interfaces:**
- Consumes: 最新 `origin/main`，当前远程主线数据库版本 13。
- Produces: 不含旧分支冲突迁移的新修复分支 `codex/m5-m8-complete-repair`。

- [x] **Step 1: 从最新主线创建修复分支**

```powershell
git fetch origin
git switch -c codex/m5-m8-complete-repair origin/main
```

- [x] **Step 2: 记录旧分支中可以移植和必须丢弃的内容**

```text
移植：已有的边界测试、Q-matrix 观测转换思路、薄弱知识点排序测试。
丢弃：SCHEMA_VERSION=4 迁移、任何 ON CONFLICT DO UPDATE 历史写入、答对率冒充 2PL、内存 paper context。
```

- [x] **Step 3: 运行主线基线测试**

```powershell
python -m pytest tests -q
```

Expected: 主线既有测试全部通过；若主线本身失败，先单独记录并修复基线，不把失败混入 M5/M8 改动。

- [x] **Step 4: 提交基线说明**

```powershell
git add docs/superpowers/plans/2026-08-12-m5-m8-complete-repair.md
git commit -m "docs: plan complete M5 M8 repair"
```

---

### Task 2: 建立权威的试卷、量规和学习观测链

**Files:**
- Create: `src/course_insight/modules/m8_assessment_scoring/paper_record.py`
- Create: `src/course_insight/modules/m8_assessment_scoring/observation_builder.py`
- Modify: `src/course_insight/contracts/assessment.py`
- Modify: `src/course_insight/contracts/learning_models.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Create: `tests/factories/m5_m8.py`
- Test: `tests/unit/test_m8_observation_builder.py`
- Test: `tests/contract/test_m5_m8_contracts.py`

**Interfaces:**
- Consumes: `AssessmentPaper`、`ScoringResultBundle`、`KnowledgeBundle`。
- Produces: `FrozenAssessmentRecord` 和 `LearningObservationBatch`。

Exact types:

- `FrozenAssessmentRecord(paper: AssessmentPaper, course_id: str, class_id: str, frozen_rubrics: list[Rubric])`
- `ModelObservationPolicy(version: str, correct_threshold: float, allowed_item_types: frozenset[str])`
- `build_observation_batch(record: FrozenAssessmentRecord, bundle: ScoringResultBundle) -> LearningObservationBatch`
- `M8AssessmentService.build_observation_batch(paper_id: str, bundle: ScoringResultBundle) -> LearningObservationBatch`：从 Repository 读取冻结记录后调用构造器。
- `LearningObservation.response_outcome: Literal["correct", "incorrect"]` 和 `outcome_policy_version: str`：保证三个模型使用同一、可追溯的二值结果。

- [x] **Step 1: 写失败测试，证明不能再通过题目实例命名规则猜测题目身份**

```python
def test_observation_uses_frozen_item_identity_not_instance_name():
    paper = paper_with_instance_id("random-instance-88", item_id="item_2")
    batch = build_observation_batch(frozen_record(paper), scoring_bundle(paper))
    assert batch.observations[0].item_id == "item_2"
    assert batch.observations[0].concept_ids == ["concept_2"]
```

- [x] **Step 2: 写失败测试，证明量规升级不能改变旧试卷评分依据**

```python
def test_old_paper_uses_its_frozen_rubric_version():
    record = frozen_record_with_rubric(version="1.0.0")
    live_bundle = knowledge_with_rubric(version="2.0.0")
    preparation = service.prepare_scoring(record.paper, submission(), live_bundle)
    assert preparation.rubric_scoring_tasks[0].rubric.version == "1.0.0"
```

- [x] **Step 3: 实现冻结记录与观测构造器**

```python
items = {item.item_instance_id: item for item in record.paper.all_items()}
for audit in latest_audits(bundle):
    item = items[audit.item_instance_id]
    observations.append(LearningObservation(
        observation_id=f"obs_{audit.audit_id}_v{audit.audit_version}",
        learner_id=bundle.learner_id,
        course_id=record.course_id,
        class_id=record.class_id,
        attempt_id=bundle.attempt_id,
        item_id=item.item_id,
        item_version=item.item_version,
        concept_ids=list(item.concept_ids),
        source_audit_id=audit.audit_id,
        source_audit_version=audit.audit_version,
        score=audit.total_score,
        max_score=audit.max_score,
        response_outcome=observation_policy.classify(
            score=audit.total_score,
            max_score=audit.max_score,
        ),
        outcome_policy_version=observation_policy.version,
        source_audit_id=audit.audit_id,
        source_audit_version=audit.audit_version,
        occurred_at=audit.created_at,
    ))
```

Repository 同时提供 `insert_or_get_paper_record(record: FrozenAssessmentRecord) -> FrozenAssessmentRecord` 与 `get_paper_record(paper_id: str) -> FrozenAssessmentRecord | None`；对外旧的 `get_paper()` 继续只返回其中的试卷，避免破坏消费者。

- [x] **Step 4: 重新生成契约 Schema 并运行契约测试**

```powershell
python scripts/generate_contract_schemas.py
python -m pytest tests/contract/test_m5_m8_contracts.py tests/unit/test_m8_observation_builder.py -q
```

- [x] **Step 5: 提交权威数据链**

```powershell
git add src/course_insight/contracts src/course_insight/modules/m8_assessment_scoring contracts/schemas tests
git commit -m "fix: bind M5 observations to frozen M8 evidence"
```

---

### Task 3: 修复 M5 逐题诊断、班级聚合和原子历史

**Files:**
- Modify: `src/course_insight/modules/m5_learner_class_state/update_policy.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/aggregation.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/repository.py`
- Modify: `src/course_insight/infrastructure/sqlite/migrations.py`
- Modify: `src/course_insight/infrastructure/sqlite/m5_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/m5_repository.py`
- Create: `src/course_insight/infrastructure/postgresql/migrations/0014_m5_m8_model_runtime.sql`
- Test: `tests/unit/test_m5_diagnosis_mapping.py`
- Test: `tests/unit/test_m5_class_replacement.py`
- Test: `tests/integration/test_m5_atomic_update.py`

**Interfaces:**
- Consumes: 权威 `LearningObservationBatch`、旧学习者状态、同班所有最新学习者状态。
- Produces: 正确的逐题诊断、唯一班级快照、一次事务保存的 `StateUpdateResult`。

Exact signatures:

- `aggregate_all(learner_states: list[LearnerStateSnapshot], policy: StatePolicy, *, class_version: int) -> ClassStateSnapshot`
- `list_latest_learner_states(course_id: str, class_id: str) -> list[LearnerStateSnapshot]`

- [x] **Step 1: 写四个失败测试**

```python
def test_each_item_uses_its_own_q_matrix_concepts(policy, bundle, knowledge, observations):
    result = policy.build_diagnosis(bundle, knowledge, observations)
    actual = {item.item_instance_id: item.concept_ids for item in result.item_diagnoses}
    assert actual == {"instance_1": ["concept_1"], "instance_2": ["concept_2"]}

def test_returning_learner_replaces_only_own_class_contribution(aggregator, state_policy):
    result = aggregator.aggregate_all(
        [learner_state("A", 0.4), learner_state("B", 0.8)],
        state_policy,
        class_version=3,
    )
    assert result.concept_status[0].mean_mastery_probability == pytest.approx(0.6)

def test_two_learners_never_share_class_snapshot_id(aggregator, state_policy):
    first = aggregator.aggregate_all([learner_state("A", 0.2)], state_policy, class_version=1)
    second = aggregator.aggregate_all(
        [learner_state("A", 0.2), learner_state("B", 0.8)],
        state_policy,
        class_version=2,
    )
    assert first.snapshot_id != second.snapshot_id

def test_failed_atomic_update_leaves_no_partial_rows(
    repository,
    complete_state_result,
    monkeypatch,
):
    def fail_class_insert(*args, **kwargs):
        raise RuntimeError("injected class insert failure")

    monkeypatch.setattr(repository, "_insert_or_validate_class", fail_class_insert)
    with pytest.raises(RuntimeError):
        repository.insert_or_get_state_update(complete_state_result)
    assert repository.get_state_update(complete_state_result.diagnosis_result.attempt_id) is None
```

Expected assertions:

```text
题目1 -> concept_1；题目2 -> concept_2。
0.2、0.8 两名学生中第一名更新为 0.4 后，班级均值为 0.6。
每次班级更新产生不同 snapshot_id。
任一步骤故障后，学生状态、班级状态、处理水位均保持更新前状态。
```

- [x] **Step 2: 改为根据同班最新学生状态重新聚合**

```python
states_by_learner = {state.learner_id: state for state in persisted_states}
states_by_learner[current.learner_id] = current
class_state = aggregate_all(
    list(states_by_learner.values()),
    policy,
    class_version=next_class_version,
)
```

- [x] **Step 3: 使用课程、班级、学习者完整范围保存水位和版本**

```text
processed key = (course_id, class_id, learner_id, audit_id, audit_version)
learner version key = (course_id, class_id, learner_id, state_version)
class version key = (course_id, class_id, class_version)
```

- [x] **Step 4: 在主线迁移链末尾建立模型运行历史表**

```text
SQLite 从主线版本 13 增加到版本 14，不改写版本 1—13。
PostgreSQL 新增 0014_m5_m8_model_runtime.sql，不修改 0001—0013。
本迁移一次建立 m5_learning_observations、m5_dina_models、m5_bkt_models、
m5_knowledge_traces、m8_irt_calibration_runs、m8_irt_parameter_sets、
m8_ability_estimates、m8_adaptive_selections、m8_calibration_reviews。
```

- [x] **Step 5: 在一个事务中保存完整状态更新**

```python
repository.insert_or_get_state_update(
    result,
    expected_previous_class_snapshot_id=previous_snapshot_id,
)
```

同一版本、相同内容返回原记录；同一版本、不同内容抛出 `STATE_VERSION_CONFLICT`。

- [x] **Step 6: 同时验证 SQLite 和 PostgreSQL**

```powershell
python -m pytest tests/unit/test_m5_diagnosis_mapping.py tests/unit/test_m5_class_replacement.py tests/integration/test_m5_atomic_update.py tests/unit/test_postgres_m5_repository.py -q
```

- [x] **Step 7: 提交 M5 基础修复和模型历史迁移**

```powershell
git add src/course_insight/modules/m5_learner_class_state src/course_insight/infrastructure tests
git commit -m "fix: make M5 state updates scoped atomic and correct"
```

---

### Task 4: 修复 M8 组卷、重启恢复、时间和不可变历史

**Files:**
- Modify: `src/course_insight/modules/m8_assessment_scoring/paper_generator.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/repository.py`
- Modify: `src/course_insight/infrastructure/sqlite/m8_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/m8_repository.py`
- Test: `tests/unit/test_m8_weighted_blueprint.py`
- Test: `tests/integration/test_m8_restart_recovery.py`
- Test: `tests/integration/test_m8_append_only_history.py`

**Interfaces:**
- Consumes: 蓝图概念权重、冻结试卷记录、真实时钟。
- Produces: 严格满足蓝图的试卷、重启后可继续的评分、不可覆盖的历史。

Exact signature: `allocate_concept_targets(concept_weights: dict[str, float], item_count: int) -> dict[str, int]`.

- [x] **Step 1: 写概念权重失败测试**

```python
def test_ten_items_follow_ten_ninety_concept_weights():
    paper = generate_paper(weights={"c1": 0.1, "c2": 0.9}, item_count=10)
    assert assigned_quota_counts(paper) == {"c1": 1, "c2": 9}
```

- [x] **Step 2: 实现最大余数配额和确定性回溯选题**

```text
1. weight * item_count 后取整数部分。
2. 剩余名额按小数余数从大到小分配，概念 ID 负责稳定排序。
3. 每道多概念题只分配给一个当前缺口最大的配额桶，禁止重复计数。
4. 找不到满足题型、分值、难度、锚题和概念配额的组合时，返回 BLUEPRINT_UNSATISFIABLE。
```

- [x] **Step 3: 写并修复重启恢复测试**

```python
paper = service_a.generate_paper(task_plan, knowledge_bundle, None, None)
preparation = service_a.prepare_scoring(paper, submission, knowledge_bundle)
service_b = create_service_with_same_database()
bundle = service_b.finalize_scoring(preparation, rubric_results)
assert bundle.learning_events[0].course_id == "course_1"
```

`service_b` 必须从 `FrozenAssessmentRecord` 读取课程、班级和量规，不再依赖内存字典。

- [x] **Step 4: 写并修复不可变历史测试**

```python
repository.insert_or_get_paper(original_record)
with pytest.raises(PAPER_VERSION_CONFLICT):
    repository.insert_or_get_paper(changed_same_identity)

repository.save_score_audit(original_audit)
with pytest.raises(SCORE_AUDIT_VERSION_CONFLICT):
    repository.save_score_audit(changed_same_version)
```

- [x] **Step 5: 更换生产默认时钟**

```python
self._clock = clock if clock is not None else SystemUTCClock()
```

`FixedClock` 仅保留在 `stubs.py` 和测试 fixtures 中。

- [x] **Step 6: 运行 M8 基础测试并提交**

```powershell
python -m pytest tests/unit/test_m8_weighted_blueprint.py tests/integration/test_m8_restart_recovery.py tests/integration/test_m8_append_only_history.py tests/unit/test_postgres_m8_repository.py -q
git add src/course_insight/modules/m8_assessment_scoring src/course_insight/infrastructure tests
git commit -m "fix: make M8 papers scoring and history durable"
```

**补充验收（2026-08-12）：** 无概念权重的分区现已按题数、总分和锚题执行
确定性组合搜索，不再因为题库首题分值不合适而错报无解。专项测试 84 项、真实
PostgreSQL 测试 4 项、全量测试 1306 项均通过，整体覆盖率为 87.57%。详细过程见
`docs/superpowers/plans/2026-08-12-m8-unweighted-blueprint-selection-repair.md`。

---

### Task 5: 实现真实 DINA 认知诊断

**Files:**
- Create: `src/course_insight/modules/m5_learner_class_state/dina.py`
- Modify: `src/course_insight/contracts/learning_models.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/repository.py`
- Test: `tests/unit/test_m5_dina.py`
- Test: `tests/model_validation/test_dina_recovery.py`

**Interfaces:**
- Consumes: 二值化后的学习观测、Q-matrix、模型配置。
- Produces: 版本化 DINA 参数和每个知识点的掌握后验概率。

Exact signatures:

- `DinaEngine.fit(cohort: list[LearningObservationBatch], q_matrix: list[QMatrixEntry]) -> DinaModelArtifact`
- `DinaEngine.infer(model: DinaModelArtifact, batch: LearningObservationBatch) -> CognitiveDiagnosisResult`

- [x] **Step 1: 写 DINA 公式级单元测试**

```python
def test_mastered_profile_uses_one_minus_slip(dina_engine):
    assert dina_engine.response_probability(capable=True, slip=0.1, guess=0.2) == pytest.approx(0.9)

def test_unmastered_profile_uses_guess_probability(dina_engine):
    assert dina_engine.response_probability(capable=False, slip=0.1, guess=0.2) == pytest.approx(0.2)

def test_q_matrix_requires_all_attributes_for_and_gate(dina_engine):
    assert not dina_engine.is_capable(profile={"c1": True, "c2": False}, required={"c1", "c2"})

def test_posterior_probabilities_are_normalized(dina_engine, fitted_dina, learner_batch):
    posterior = dina_engine.profile_posterior(fitted_dina, learner_batch)
    assert sum(posterior.values()) == pytest.approx(1.0)
```

- [x] **Step 2: 实现显式二值化策略**

```python
is_correct = observation.score / observation.max_score >= policy.correct_threshold
```

阈值必须来自版本化策略；没有策略时拒绝训练，不得写死在算法内部。

- [x] **Step 3: 持久化并读取课程范围内的训练观测**

```python
repository.insert_or_get_learning_observation_batch(observation_batch)
cohort = repository.list_learning_observations(
    course_id=observation_batch.observations[0].course_id,
    class_id=observation_batch.observations[0].class_id,
)
```

同一审计版本只保存一次；DINA 至少需要 200 名不同学生、每道题至少 50 条且同时含正确和错误作答，否则返回 `INSUFFICIENT_MODEL_DATA`。

- [x] **Step 4: 实现 EM 拟合和后验推断**

```text
E 步：计算每名学生对各属性掌握组合的后验概率。
M 步：按后验权重更新每道题的 slip、guess 和属性先验。
收敛：连续两次对数似然差小于 1e-6，最多 200 轮。
边界：slip、guess 限制在 [0.01, 0.40]，避免退化为无意义参数。
维度：按 Q-matrix 的连通知识点分量分别推断；不超过 12 个知识点的分量使用精确后验，超过 12 个知识点的分量使用均值场变分 EM，并记录 `inference_mode="variational"`、ELBO 和收敛轮数，避免 2^K 状态空间耗尽内存且不丢弃功能。
```

- [x] **Step 5: 用固定合成数据验证模型恢复能力**

```text
seed=20260812；500 名学生；20 道题；4 个知识点。
每知识点掌握分类 AUC >= 0.85。
slip 和 guess 的平均绝对误差 <= 0.08。
相同输入和 seed 必须生成相同模型版本和结果。
增加一个 20 知识点连通分量用例，验证变分路径产生有限概率、ELBO 不下降且内存峰值低于 2 GB。
```

- [x] **Step 6: 运行测试并提交**

```powershell
python -m pytest tests/unit/test_m5_dina.py tests/model_validation/test_dina_recovery.py -q
git add src/course_insight/modules/m5_learner_class_state/dina.py src/course_insight/contracts tests
git commit -m "feat: implement DINA cognitive diagnosis"
```

---

### Task 6: 实现真实 BKT 知识追踪

**Files:**
- Create: `src/course_insight/modules/m5_learner_class_state/bkt.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/repository.py`
- Test: `tests/unit/test_m5_bkt.py`
- Test: `tests/model_validation/test_bkt_recovery.py`

**Interfaces:**
- Consumes: 按发生时间排序的学生—知识点作答序列。
- Produces: 每个知识点的 `prior`、`learn`、`guess`、`slip` 参数和最新掌握概率。

Exact signatures:

- `BktEngine.fit(sequences: list[ConceptResponseSequence]) -> BktModelArtifact`
- `BktEngine.update(model: BktModelArtifact, sequence: ConceptResponseSequence) -> KnowledgeTraceSnapshot`

- [x] **Step 1: 写正确、错误和多次作答的概率更新测试**

```python
def test_correct_answer_raises_mastery_probability(bkt_engine, bkt_model):
    before = bkt_engine.update(bkt_model, response_sequence([]))
    after = bkt_engine.update(bkt_model, response_sequence([True]))
    assert after.concept_probabilities["c1"] > before.concept_probabilities["c1"]

def test_incorrect_answer_reduces_posterior_before_transition(bkt_engine, bkt_model):
    posterior = bkt_engine.observation_update(prior=0.5, correct=False, guess=0.2, slip=0.1)
    assert posterior < 0.5

def test_order_of_answers_changes_trace(bkt_engine, bkt_model):
    first = bkt_engine.update(bkt_model, response_sequence([True, False]))
    second = bkt_engine.update(bkt_model, response_sequence([False, True]))
    assert first.concept_probabilities != second.concept_probabilities

def test_replay_watermark_prevents_double_update(bkt_service, learner_batch):
    first = bkt_service.update_trace(learner_batch)
    replay = bkt_service.update_trace(learner_batch)
    assert replay == first
```

- [x] **Step 2: 实现四参数 BKT 前向更新和 Baum-Welch 拟合**

```text
参数：初始掌握 prior、学习转移 learn、猜测 guess、失误 slip。
每个知识点独立维护时序状态。
观测必须先按 occurred_at、attempt_id、observation_id 稳定排序。
同一 source_audit_id + source_audit_version 只能消费一次。
每个知识点至少需要 100 名学生、每名学生至少 5 条有序观测后才拟合课程参数；不足时返回 `INSUFFICIENT_MODEL_DATA`，不得套用伪造参数。
```

- [x] **Step 3: 持久化模型版本和学生追踪水位**

```python
repository.insert_or_get_bkt_model(model)
repository.insert_or_get_knowledge_trace(trace)
```

- [x] **Step 4: 用合成序列验收**

```text
seed=20260812；300 名学生；每人 30 次观测；4 个知识点。
参数平均绝对误差 <= 0.08。
一步预测 log loss 至少比固定全局答对率基线低 10%。
服务重启后继续一条新观测，与不中断运行的结果完全一致。
```

- [x] **Step 5: 运行测试并提交**

```powershell
python -m pytest tests/unit/test_m5_bkt.py tests/model_validation/test_bkt_recovery.py -q
git add src/course_insight/modules/m5_learner_class_state tests
git commit -m "feat: implement Bayesian knowledge tracing"
```

---

### Task 7: 让真实模型驱动 M5 学生状态

**Files:**
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/update_policy.py`
- Modify: `src/course_insight/contracts/state.py`
- Modify: `src/course_insight/application/coordinator.py`
- Test: `tests/integration/test_m8_m5_model_chain.py`

**Interfaces:**
- Consumes: M8 权威观测、DINA 结果、BKT 追踪结果。
- Produces: 带模型版本证据的学生状态和班级状态。

Exact signatures and field:

- `run_learning_models(observation_batch: LearningObservationBatch, knowledge_bundle: KnowledgeBundle) -> LearningModelRun`
- `update_state(scoring_result_bundle: ScoringResultBundle, knowledge_bundle: KnowledgeBundle, observation_batch: LearningObservationBatch, learning_model_run: LearningModelRun, previous_learner_state_snapshot: LearnerStateSnapshot | None, previous_class_state_snapshot: ClassStateSnapshot | None, state_policy_path: Path) -> StateUpdateResult`
- `LearnerStateSnapshot.model_run_id: str | None = None`：兼容旧快照；本次上线后产生的新快照必须填写实际模型运行版本。

- [x] **Step 1: 写完整链路失败测试**

```python
paper = m8.generate_paper(task_plan, knowledge_bundle, None, None)
preparation = m8.prepare_scoring(paper, submission, knowledge_bundle)
bundle = m8.finalize_scoring(preparation, rubric_results)
observations = m8.build_observation_batch(bundle.paper_id, bundle)
models = m5.run_learning_models(observations, knowledge_bundle)
state = m5.update_state(
    bundle,
    knowledge_bundle,
    observations,
    models,
    previous_learner_state,
    previous_class_state,
    state_policy_path,
)
assert models.status == "completed"
assert state.learner_state_snapshot.model_run_id == models.run_id
```

- [x] **Step 2: 明确两个模型的业务分工**

```text
DINA：回答“学生目前可能掌握了哪些知识属性”，用于诊断和薄弱点排序。
BKT：回答“学生随时间学习后当前掌握概率是多少”，作为 ConceptState.mastery_probability。
两者的模型版本、观测水位和数据量写入状态证据；不做无依据的简单平均。
```

- [x] **Step 3: coordinator 使用真实评分观测，不再构造空批次**

```python
observation_batch = self._m8.build_observation_batch(
    scoring_result.paper_id,
    scoring_result,
)
learning_model_run = self._m5.run_learning_models(
    observation_batch,
    knowledge_bundle,
)
```

- [x] **Step 4: 运行链路测试并提交**

```powershell
python -m pytest tests/integration/test_m8_m5_model_chain.py -q
git add src/course_insight/application src/course_insight/contracts src/course_insight/modules/m5_learner_class_state tests
git commit -m "feat: drive M5 state from DINA and BKT evidence"
```

---

### Task 8: 实现真正的 2PL IRT 标定

**Files:**
- Create: `src/course_insight/modules/m8_assessment_scoring/irt_2pl.py`
- Modify: `pyproject.toml`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/contracts/learning_models.py`
- Test: `tests/unit/test_m8_irt_2pl.py`
- Test: `tests/model_validation/test_irt_2pl_recovery.py`

**Interfaces:**
- Consumes: 多名学生对多道题目的权威 `response_outcome`，不在 IRT 内部重新猜测二值阈值。
- Produces: 题目难度、题目区分度、收敛指标和 `shadow` 参数集。

Exact signatures:

- `TwoPLCalibrator.fit(observations: list[LearningObservation], requested_at: datetime) -> CalibrationRunResult`
- `TwoPLCalibrator.estimate_ability(parameter_set: IRTParameterSet, responses: list[LearningObservation]) -> AbilityEstimate`

- [x] **Step 1: 增加数值计算依赖并写公式测试**

```toml
"numpy>=2.0,<3",
"scipy>=1.13,<2",
```

```python
def test_higher_ability_increases_success_probability(two_pl):
    assert two_pl.probability(theta=1.0, discrimination=1.2, difficulty=0.0) > two_pl.probability(
        theta=-1.0, discrimination=1.2, difficulty=0.0
    )

def test_higher_discrimination_increases_information_near_difficulty(two_pl):
    assert two_pl.information(theta=0.0, discrimination=2.0, difficulty=0.0) > two_pl.information(
        theta=0.0, discrimination=0.5, difficulty=0.0
    )

def test_single_response_never_claims_convergence(calibrator, one_observation):
    result = calibrator.fit([one_observation], FIXED_TIME)
    assert not result.converged
    assert result.status == "failed"
```

- [x] **Step 2: 实现边际最大似然 2PL**

```text
使用 21 点 Gauss-Hermite 求积表示学生能力分布。
E 步计算每名学生在能力节点上的后验权重。
M 步用 L-BFGS-B 分题优化 discrimination 和 difficulty。
discrimination 范围 [0.20, 3.00]；difficulty 范围 [-4.00, 4.00]。
对数似然差 < 1e-6 且参数最大变化 < 1e-4 才算收敛，最多 200 轮。
```

- [x] **Step 3: 设置真实的数据充分性门槛**

```text
至少 200 名不同学生。
每道题至少 50 条有效作答，且同时出现正确和错误。
至少 10 道可标定题。
不足时返回明确的 INSUFFICIENT_CALIBRATION_DATA，不生成参数、不声称收敛。
```

- [x] **Step 4: 用合成数据验证参数恢复**

```text
seed=20260812；1000 名学生；30 道题。
难度参数相关系数 >= 0.90，RMSE <= 0.35。
区分度参数相关系数 >= 0.85，RMSE <= 0.30。
所有收敛结果必须包含 log_likelihood、AIC、BIC、iteration_count。
```

- [x] **Step 5: 运行测试并提交**

```powershell
python -m pytest tests/unit/test_m8_irt_2pl.py tests/model_validation/test_irt_2pl_recovery.py -q
git add pyproject.toml src/course_insight/modules/m8_assessment_scoring/irt_2pl.py src/course_insight/contracts tests
git commit -m "feat: implement validated 2PL calibration"
```

---

### Task 9: 实现 IRT 参数持久化、质量审核和能力估计

**Files:**
- Modify: `src/course_insight/modules/m8_assessment_scoring/repository.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/infrastructure/sqlite/m8_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/m8_repository.py`
- Test: `tests/integration/test_m8_model_approval.py`

**Interfaces:**
- Consumes: `CalibrationRunResult`、`ModelQualityReport`、`CalibrationReviewDecision`。
- Produces: 不可变 shadow/approved/rejected 参数版本和 EAP 能力估计。

Exact signature: `apply_calibration_review(run_id: str, quality_report: ModelQualityReport, decision: CalibrationReviewDecision) -> IRTParameterSet`.

- [x] **Step 1: 写迁移与追加历史失败测试**

```python
def test_parameter_versions_are_append_only(repository, shadow_set):
    repository.insert_or_get_parameter_set(shadow_set)
    with pytest.raises(RuntimeError, match="parameter-set conflict"):
        repository.insert_or_get_parameter_set(shadow_set.model_copy(update={"sample_size": 999}))

def test_rejected_parameter_set_cannot_be_activated(service, rejected_set):
    with pytest.raises(DomainError, match="approved"):
        service.activate_parameter_set(rejected_set.parameter_set_id)

def test_restart_recovers_approved_parameter_set(service_factory, approved_set):
    service_factory().store_parameter_set(approved_set)
    recovered = service_factory().get_active_parameter_set(approved_set.parameter_set_id)
    assert recovered == approved_set

def test_sqlite_and_postgres_return_same_model_history(sqlite_repo, postgres_repo, parameter_history):
    for record in parameter_history:
        sqlite_repo.insert_or_get_parameter_set(record)
        postgres_repo.insert_or_get_parameter_set(record)
    assert sqlite_repo.list_parameter_sets() == postgres_repo.list_parameter_sets()
```

- [x] **Step 2: 接入 Task 3 已创建的模型运行表**

```text
m5_learning_observations
m5_dina_models
m5_bkt_models
m5_knowledge_traces
m8_irt_calibration_runs
m8_irt_parameter_sets
m8_ability_estimates
m8_adaptive_selections
m8_calibration_reviews
```

验证所有表均使用完整作用域、模型版本和 payload checksum；SQLite 与 PostgreSQL Repository 对同一身份、同一版本、不同内容都必须拒绝写入。

- [x] **Step 3: 实现审核状态机**

```text
shadow + 质量报告 ready + 教师 approve -> approved
shadow + 教师 reject -> rejected
shadow + defer -> 保持 shadow
approved/rejected -> 禁止再次改写原版本，只能创建新标定版本
```

- [x] **Step 4: 实现 EAP 能力估计**

```text
只使用 approved 2PL 参数。
使用同一组 21 点能力节点计算 EAP theta 和 posterior standard error。
全对或全错仍返回有限能力和有限标准误，不允许无穷值。
```

- [x] **Step 5: 运行双数据库测试并提交**

```powershell
python -m pytest tests/integration/test_m8_model_approval.py tests/unit/test_postgres_m8_repository.py tests/integration/test_sqlite_to_postgres_migration.py -q
git add src/course_insight/infrastructure src/course_insight/modules/m8_assessment_scoring tests
git commit -m "feat: persist and approve IRT model versions"
```

---

### Task 10: 实现自适应选题

**Files:**
- Create: `src/course_insight/modules/m8_assessment_scoring/adaptive_selector.py`
- Modify: `src/course_insight/contracts/learning_models.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/repository.py`
- Test: `tests/unit/test_m8_adaptive_selector.py`
- Test: `tests/model_validation/test_adaptive_efficiency.py`

**Interfaces:**
- Consumes: approved 2PL 参数、当前能力估计、可用题目池、已作答题目、概念配额。
- Produces: 满足约束且信息量最高的下一批题目。

Exact contracts and signature:

- `AdaptiveSelectionPolicy.max_item_exposure_rate: float = 0.20`、`difficulty_range: tuple[float, float] = (-4.0, 4.0)`：使用默认值兼容旧策略。
- `ItemExposureSnapshot(parameter_set_id: str, total_sessions: int, item_administered_counts: dict[str, int])`。
- `select_adaptive_items(policy: AdaptiveSelectionPolicy, ability_estimate: AbilityEstimate, parameter_set: IRTParameterSet, candidate_items: list[ItemCard], administered_item_ids: frozenset[str], exposure_snapshot: ItemExposureSnapshot, requested_at: datetime) -> AdaptiveSelectionResult`。

- [x] **Step 1: 写选题约束失败测试**

```python
def test_selects_highest_information_available_item(selector, selection_case):
    result = selector.select(**selection_case)
    assert result.item_ids == ["highest_information_item"]

def test_never_repeats_administered_item(selector, selection_case):
    updated_case = {
        **selection_case,
        "administered_item_ids": frozenset({"highest_information_item"}),
    }
    result = selector.select(**updated_case)
    assert "highest_information_item" not in result.item_ids

def test_rejects_shadow_parameters(selector, selection_case, shadow_set):
    updated_case = {**selection_case, "parameter_set": shadow_set}
    with pytest.raises(DomainError, match="approved"):
        selector.select(**updated_case)

def test_meets_concept_quotas_before_free_selection(selector, quota_case):
    result = selector.select(**quota_case)
    assert item_concepts(result.item_ids)[0] == "quota_concept"

def test_same_input_has_deterministic_tie_breaking(selector, tied_case):
    assert selector.select(**tied_case).item_ids == selector.select(**tied_case).item_ids

def test_respects_item_exposure_cap(selector, exposed_case):
    result = selector.select(**exposed_case)
    assert "overexposed_item" not in result.item_ids
```

- [x] **Step 2: 实现 2PL Fisher 信息量**

```python
p = sigmoid(a * (theta - b))
information = a * a * p * (1.0 - p)
```

- [x] **Step 3: 按功能约束选题**

```text
先过滤非 approved 参数题、已作答题、难度超出策略范围的题，以及历史曝光率达到上限的题。
先补足尚未完成的概念配额，再按信息量从高到低选题。
信息量相同按 item_id、item_version 排序，保证可复现。
没有合格题目时返回 ADAPTIVE_POOL_EXHAUSTED，不返回伪造题目。
每完成一题，先用真实作答更新并持久化 AbilityEstimate，再基于新能力选择下一题。
```

- [x] **Step 4: 做自适应效率模拟验收**

```text
seed=20260812；500 名模拟学生；200 道 approved 题目。
概念配额满足率 100%，重复题率 0%，曝光上限违反率 0%。
达到相同能力标准误时，平均用题数比固定顺序测验至少减少 20%。
```

- [x] **Step 5: 运行测试并提交**

```powershell
python -m pytest tests/unit/test_m8_adaptive_selector.py tests/model_validation/test_adaptive_efficiency.py -q
git add src/course_insight/contracts src/course_insight/modules/m8_assessment_scoring tests
git commit -m "feat: implement constrained adaptive item selection"
```

---

### Task 11: 完成教师复核、评分状态与 M5 消费边界

**Files:**
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/application/coordinator.py`
- Test: `tests/integration/test_m8_review_to_m5.py`

**Interfaces:**
- Consumes: 教师批准、修改或拒绝的评分版本。
- Produces: 只有有效最终评分才能进入 M5 的受控链路。

- [x] **Step 1: 写教师复核并发和拒绝测试**

```python
def test_review_requires_exact_current_audit_version_and_checksum(service, stale_review):
    with pytest.raises(DomainError, match="REVIEW_VERSION_CONFLICT"):
        service.apply_teacher_review(stale_review)

def test_rejected_score_does_not_update_m5(coordinator, rejected_review):
    result = coordinator.apply_review(rejected_review)
    assert result.state_update is None

def test_teacher_override_creates_new_audit_version(service, override_review, original_audit):
    reviewed = service.apply_teacher_review(override_review)
    assert reviewed.audit_version == original_audit.audit_version + 1

def test_same_review_replay_returns_same_result(service, override_review):
    first = service.apply_teacher_review(override_review)
    assert service.apply_teacher_review(override_review) == first
```

- [x] **Step 2: 实现精确并发保护**

```text
复核请求必须携带 expected_audit_version 和 expected_audit_checksum。
数据库事务内重新读取当前版本；版本或 checksum 任一不一致即返回 REVIEW_VERSION_CONFLICT。
教师修改产生 audit_version + 1；原版本永不修改。
```

- [x] **Step 3: 建立 M5 消费规则**

```text
approved 或无需复核：允许生成学习观测。
teacher_override：只消费新版本，旧版本保持历史但不重复更新。
rejected：不生成可进入 M5 的学习观测，并记录拒绝原因。
```

- [x] **Step 4: 运行链路测试并提交**

```powershell
python -m pytest tests/integration/test_m8_review_to_m5.py -q
git add src/course_insight/application src/course_insight/modules tests
git commit -m "fix: govern reviewed scores before M5 consumption"
```

---

### Task 12: 完整验收、文档校正与发布门槛

**Files:**
- Modify: `docs/M5_M8_task_guide.md`
- Modify: `docs/audit_verification_report.md`
- Modify: `src/course_insight/modules/m5_learner_class_state/README.md`
- Modify: `src/course_insight/modules/m8_assessment_scoring/README.md`
- Test: `tests/e2e/test_m5_m8_full_learning_cycle.py`

**Interfaces:**
- Consumes: Tasks 1—11 的所有产物。
- Produces: 可合并、可重启、可追溯且模型指标达标的 M5/M8 完整实现。

- [x] **Step 1: 写端到端业务测试**

```text
教师发布知识包和蓝图 -> M8 按比例组卷 -> 学生作答 -> 客观/主观评分 -> 教师复核
-> M8 生成权威观测 -> M5 DINA/BKT -> 学生及班级状态 -> IRT shadow 标定
-> M9 质量报告与教师批准 -> 能力估计 -> 自适应选择下一题。
```

测试中途分别重启 M5 和 M8 服务，最终结果必须与不中断运行完全一致。

- [x] **Step 2: 运行完整测试、覆盖率和静态健康检查**

```powershell
python -m compileall src
python -m pytest tests -q --cov=course_insight --cov-report=term-missing --cov-fail-under=80
python -m pip check
git diff --check origin/main...HEAD
```

Expected: 全部通过；M5/M8 新文件逐个检查覆盖率不低于 90%。

- [x] **Step 3: 校正文档中的功能状态**

```text
删除“空实现”说明。
删除“答对率代理就是 2PL”的描述。
明确 DINA/BKT 的输入、模型版本、数据门槛和输出。
明确 IRT shadow -> 质量审核 -> 教师批准 -> adaptive 的启用顺序。
把已复现问题和对应回归测试逐项关联。
```

- [x] **Step 4: 做最终数据兼容验证**

```powershell
python -m pytest tests/integration/test_sqlite_to_postgres_migration.py tests/integration/test_postgres_foundation_live.py -q
```

Expected: 旧数据库可以按版本升级；SQLite 导入 PostgreSQL 后，试卷、评分、状态和模型历史数量及 checksum 一致。

- [x] **Step 5: 提交最终验收结果**

```powershell
git add docs src tests contracts pyproject.toml
git commit -m "test: verify complete M5 M8 learning workflow"
```

---

## Delivery Order and Gates

| 阶段 | 包含任务 | 通过条件 | 未通过时的处理 |
|---|---:|---|---|
| A. 数据可靠性 | 1—4 | 逐题映射、班级均值、重启恢复、不可变历史全部通过 | 禁止开始模型接入 |
| B. M5 模型 | 5—7 | DINA/BKT 合成恢复指标达标，真实模型驱动状态 | 禁止向 M6/M9 暴露 estimated 状态 |
| C. M8 模型 | 8—10 | 2PL 收敛与恢复指标达标，只有 approved 参数能选题 | 禁止启用 adaptive |
| D. 业务闭环 | 11—12 | 复核语义、双数据库、重启和全链路通过 | 禁止合并或发布 |

## Estimated Effort

| 工作块 | 单人预计工作日 |
|---|---:|
| 主线整理、契约和基础数据链 | 3—4 |
| M5 状态与持久化修复 | 3—4 |
| M8 组卷、评分和恢复修复 | 3—4 |
| DINA 与 BKT | 6—9 |
| 2PL IRT、审核和能力估计 | 6—8 |
| 自适应选题与完整验收 | 4—6 |
| **合计** | **25—35** |

该估算包含测试和文档，不包含真实生产数据清洗时间。算法实现不需要 GPU；上述合成验收规模可在普通 4 核 CPU、16 GB 内存环境完成。

## Self-Review Result

- 已覆盖扫描报告中的 14 项明确缺陷。
- 已把 DINA、BKT、真实 2PL IRT、自适应选题及 IRT 审核发布闭环全部改为本阶段交付。
- 已明确解决旧分支落后主线和 9 个合并冲突的方式。
- 已为 SQLite、PostgreSQL、服务重启、并发写入、历史不可变和模型质量设置验收测试。
- 没有保留以固定值、普通答对率或空列表冒充完成结果的路径。
