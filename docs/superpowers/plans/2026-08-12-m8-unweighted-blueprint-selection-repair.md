# M8 无权重蓝图组卷修复实施计划

> **执行要求：** 使用 `superpowers:executing-plans` 在当前会话中单线程执行；禁止使用子智能体。所有步骤使用复选框跟踪。

**目标：** 当蓝图没有设置概念权重时，只要题库中存在同时满足题数、题型、难度、锚题、去重和总分的组合，M8 就必须稳定选出该组合。

**实现方式：** 保留现有有权重组卷逻辑，新增一个无权重的确定性回溯选择器。选择器固定保留锚题，并按稳定顺序尝试其余候选题；只有遍历后确实不存在合法组合，才返回 `BLUEPRINT_UNSATISFIABLE`。

**技术栈：** Python 3.11、Pydantic、pytest。

## 全局约束

- 仅修复 Task 4 的无权重组卷缺陷，不进入 Task 5。
- 不增加数据库迁移、契约字段或第三方依赖。
- 先写失败测试，再写实现。
- 相同输入必须得到相同试卷。
- 锚题不能被跳过，题目不能跨分区重复。
- 确实没有合法组合时，继续返回 `BLUEPRINT_UNSATISFIABLE`。
- 使用 `D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe` 执行测试。

---

### Task 1：用测试锁定错误场景

**Files:**
- Modify: `tests/unit/test_m8_weighted_blueprint.py`

**Interfaces:**
- Consumes: `PaperGenerator.generate(...)`、现有 M5/M8 测试工厂。
- Produces: 三个无权重组卷回归测试。

- [x] **Step 1：增加无权重题库构造器**

```python
def _unweighted_score_bundle(
    scores: list[float],
    *,
    item_count: int,
    section_score: float,
    anchor_indexes: list[int] | None = None,
) -> KnowledgeBundle:
    base = make_knowledge_bundle(subjective=False)
    source_item = base.items[0]
    source_q = base.q_matrix[0]
    items = [
        source_item.model_copy(
            update={
                "item_id": f"item_score_{index}",
                "answer_key": {"answer": "yes", "max_score": score},
            }
        )
        for index, score in enumerate(scores)
    ]
    anchors = anchor_indexes or []
    section = base.blueprints[0].sections[0].model_copy(
        update={
            "item_count": item_count,
            "score": section_score,
            "concept_weights": {},
            "anchor_item_ids": [items[index].item_id for index in anchors],
        }
    )
    blueprint = base.blueprints[0].model_copy(
        update={"sections": [section], "total_score": section_score}
    )
    return base.model_copy(
        update={
            "items": items,
            "blueprints": [blueprint],
            "q_matrix": [
                source_q.model_copy(update={"item_id": item.item_id})
                for item in items
            ],
        }
    )
```

- [x] **Step 2：增加“跳过错误首题、选择后续合法题”测试**

```python
def test_unweighted_generation_backtracks_to_a_valid_score_combination() -> None:
    bundle = _unweighted_score_bundle(
        [2.0, 1.0],
        item_count=1,
        section_score=1.0,
    )

    paper = PaperGenerator(FixedClock(UTC_TIME)).generate(
        make_task_plan(), bundle, None, None
    )

    assert [item.item_id for item in paper.all_items()] == ["item_score_1"]
```

- [x] **Step 3：增加“回溯时必须保留锚题”测试**

```python
def test_unweighted_generation_keeps_anchor_while_backtracking() -> None:
    bundle = _unweighted_score_bundle(
        [2.0, 2.0, 1.0],
        item_count=2,
        section_score=3.0,
        anchor_indexes=[0],
    )

    paper = PaperGenerator(FixedClock(UTC_TIME)).generate(
        make_task_plan(), bundle, None, None
    )

    assert [item.item_id for item in paper.all_items()] == [
        "item_score_0",
        "item_score_2",
    ]
```

- [x] **Step 4：增加“多个合法组合时结果稳定”测试**

```python
def test_unweighted_generation_chooses_the_first_stable_valid_combination() -> None:
    bundle = _unweighted_score_bundle(
        [1.0, 1.0],
        item_count=1,
        section_score=1.0,
    )
    generator = PaperGenerator(FixedClock(UTC_TIME))

    first = generator.generate(make_task_plan(), bundle, None, None)
    second = generator.generate(make_task_plan(), bundle, None, None)

    assert first == second
    assert first.all_items()[0].item_id == "item_score_0"
```

- [x] **Step 5：运行第一个新测试并确认修复前失败**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/unit/test_m8_weighted_blueprint.py::test_unweighted_generation_backtracks_to_a_valid_score_combination -q
```

Expected: 当前代码抛出 `BLUEPRINT_UNSATISFIABLE`，证明测试命中已复现缺陷。

---

### Task 2：实现无权重的确定性组合搜索

**Files:**
- Modify: `src/course_insight/modules/m8_assessment_scoring/paper_generator.py`
- Test: `tests/unit/test_m8_weighted_blueprint.py`

**Interfaces:**
- Consumes: 已过滤的锚题和候选题、分区题数与总分。
- Produces: `_select_unweighted_items(...) -> list[ItemCard]`。

- [x] **Step 1：新增无权重选择器**

```python
def _select_unweighted_items(
    self,
    section: BlueprintSection,
    knowledge_bundle: KnowledgeBundle,
    anchors: list[ItemCard],
    candidates: list[ItemCard],
) -> list[ItemCard]:
    ordered = [*anchors, *candidates]
    scores = [item.max_score(knowledge_bundle) for item in ordered]
    failed: set[tuple[int, int, float]] = set()

    def search(index: int, slots: int, score: float) -> list[ItemCard] | None:
        key = (index, slots, round(score, 9))
        if key in failed:
            return None
        if slots == 0:
            if math.isclose(
                score,
                section.score,
                rel_tol=0.0,
                abs_tol=_SCORE_TOLERANCE,
            ):
                return []
            failed.add(key)
            return None
        if (
            index >= len(ordered)
            or len(ordered) - index < slots
            or score > section.score + _SCORE_TOLERANCE
        ):
            failed.add(key)
            return None

        item = ordered[index]
        tail = search(index + 1, slots - 1, score + scores[index])
        if tail is not None:
            return [item, *tail]
        if index >= len(anchors):
            tail = search(index + 1, slots, score)
            if tail is not None:
                return tail
        failed.add(key)
        return None

    selected = search(0, section.item_count, 0.0)
    if selected is None:
        self._raise_section_unsatisfiable(
            section,
            "no item combination has maxima matching the section score",
        )
    return selected
```

- [x] **Step 2：让无权重分区调用新选择器**

```python
selected = (
    self._select_weighted_items(
        section,
        knowledge_bundle,
        anchors,
        ordered_candidates,
    )
    if section.concept_weights
    else self._select_unweighted_items(
        section,
        knowledge_bundle,
        anchors,
        ordered_candidates,
    )
)
```

- [x] **Step 3：保留现有最终防线**

继续保留生成后的三项检查：

```text
选中题数必须等于 item_count。
选中题目总分必须等于 section.score。
整张试卷总分必须等于 blueprint.total_score。
无权重题库数量不足时继续返回“not enough approved items”。
```

- [x] **Step 4：运行三个新测试并确认通过**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/unit/test_m8_weighted_blueprint.py -q
```

Expected: 新场景与原有有权重、锚题、无解场景全部通过。

---

### Task 3：确认没有削弱原有组卷规则

**Files:**
- Verify: `tests/unit/test_m8_weighted_blueprint.py`
- Verify: `tests/integration/test_m8_restart_recovery.py`
- Verify: `tests/integration/test_m8_append_only_history.py`
- Verify: `tests/integration/test_m5_m8_retry_concurrency.py`

**Interfaces:**
- Consumes: 修复后的 `PaperGenerator`。
- Produces: Task 4 的功能回归证据。

- [x] **Step 1：验证有权重组卷仍按比例选题**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/unit/test_m8_weighted_blueprint.py::test_ten_items_follow_ten_ninety_concept_weights -q
```

Expected: 10% / 90% 蓝图仍选择 1 道和 9 道。

- [x] **Step 2：验证确实无解时仍明确失败**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/unit/test_m8_weighted_blueprint.py::test_generator_rejects_short_pool_score_mismatch_and_impossible_quota -q
```

Expected: 题库数量不足、总分无解、概念配额无解仍返回明确错误。

- [x] **Step 3：运行 M8 完整基础回归**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/unit/test_m8_weighted_blueprint.py tests/integration/test_m8_restart_recovery.py tests/integration/test_m8_append_only_history.py tests/unit/test_postgres_m8_repository.py tests/integration/test_m5_m8_retry_concurrency.py -q
```

Expected: 0 failed；重复组卷、重复评分、重启恢复、不可变历史与并发行为不变。

---

### Task 4：重新执行前四个 Task 的正式准入检查

**Files:**
- Verify: `tests/`
- Verify: `contracts/schemas/`
- Modify after success: `docs/superpowers/plans/2026-08-12-m5-m8-complete-repair.md`

**Interfaces:**
- Consumes: 完成修复的 Task 1—4 分支。
- Produces: 是否可以进入 Task 5 的最终结论。

- [x] **Step 1：运行前四项专项测试**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/contract/test_m5_m8_contracts.py tests/unit/test_m8_observation_builder.py tests/unit/test_m5_diagnosis_mapping.py tests/unit/test_m5_class_replacement.py tests/integration/test_m5_atomic_update.py tests/unit/test_postgres_m5_repository.py tests/unit/test_m8_weighted_blueprint.py tests/integration/test_m8_restart_recovery.py tests/integration/test_m8_append_only_history.py tests/unit/test_postgres_m8_repository.py tests/integration/test_m5_m8_retry_concurrency.py -q
```

Expected: 0 failed。

- [x] **Step 2：使用临时 PostgreSQL 运行真实仓储测试**

```powershell
$env:COURSE_INSIGHT_TEST_DATABASE_URL='<临时 PostgreSQL 测试库地址>'
$env:COURSE_INSIGHT_TEST_DATABASE_NAME='course_insight_test'
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests/integration/test_postgres_m5_m9_repositories.py -q
```

Expected: 4 passed，并在测试后删除临时数据库容器。

- [x] **Step 3：运行全量测试和覆盖率门槛**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m pytest tests -q --cov=course_insight --cov-report=term-missing:skip-covered --cov-fail-under=80
```

Expected: 0 failed，整体覆盖率不低于 80%。

- [x] **Step 4：检查契约、编译和差异**

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' scripts/export_schemas.py
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m course_insight.cli export-schemas
git diff --exit-code -- contracts/schemas
& 'D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe' -m compileall -q src scripts tests
git diff --check
```

Expected: 全部退出码为 0，无契约漂移、语法错误或空白错误。

- [x] **Step 5：只有全部通过后，重新确认 Task 4 合格并提交**

```powershell
git add src/course_insight/modules/m8_assessment_scoring/paper_generator.py tests/unit/test_m8_weighted_blueprint.py docs/superpowers/plans
git commit -m "fix: backtrack unweighted M8 paper selection"
```

Expected: Task 1—4 可以正式判定合格，再进入 Task 5。
