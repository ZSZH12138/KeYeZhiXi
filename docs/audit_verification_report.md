# M5/M8 审计报告核实记录

> 核实时间：2026-08-04
> 核实基准：commit 37caf51 + 未提交的补充修复（含 M5-07/M8-10 完全修复）
> 测试状态：313 passed

---

## 一、明确缺陷核实汇总

| 编号 | 判定 | 状态 | 说明 |
|------|------|------|------|
| M5-01 | 明确缺陷 | ✅ 已修复 | 混合批次中旧审计已被过滤（`new_audit_keys = audit_keys - seen`） |
| M5-02 | 明确缺陷 | ✅ 已修复 | 审计版本与状态版本的数值比较已删除，仅使用 `(audit_id, audit_version)` |
| M5-03 | 明确缺陷 | ✅ 已修复 | `build_learner_state` 已实现前版状态加权融合（mastery、evidence_count、misconception） |
| M5-04 | 明确缺陷 | ✅ 已修复 | 班级聚合改为增量合并，新学习者折叠入加权均值，返回学习者替换而非追加 |
| M5-05 | 明确缺陷 | ✅ 已修复 | 逐题诊断已使用 Q-matrix 查找 `(item_id, item_version)` → `concept_ids` |
| M5-06 | 明确缺陷 | ✅ 已修复 | `convert_to_observations()` 已实现（commit 987ab49）；生产 coordinator 已接入 |
| M5-07 | 明确缺陷 | ✅ 已修复 | 服务从 repository 读取前版状态和已处理审计水位；getattr 防御模式已移除；SQLite 适配器已实现 |
| M8-01 | 明确缺陷 | ✅ 已修复 | `ItemInstance` 增加 `rubric_version` 字段，冻结试卷时从知识包写入量规版本 |
| M8-02 | 明确缺陷 | ✅ 已修复 | 薄弱概念（mastery < 0.5）优先排序，M5 状态参与组卷 |
| M8-03 | 明确缺陷 | ✅ 已修复 | `concept_weights` 配额追踪，选题后验证覆盖 |
| M8-04 | 明确缺陷 | ✅ 已修复 | `_validate_references` 增加 `course_package_id` 一致性校验 |
| M8-05 | 明确缺陷 | ✅ 已修复 | `ReviewPolicy.needs_review()` 双评分分歧检测已接入 |
| M8-06 | 明确缺陷 | ✅ 已修复 | checksum 已包含 `submission_id` 和 `submitted_at` |
| M8-07 | 明确缺陷 | ✅ 已修复 | course/class 身份冻结入 AssessmentPaper，从冻结试卷恢复 |
| M8-08 | 明确缺陷 | ✅ 已修复 | 事件 ID 已包含 `content_checksum`，不同内容产生不同 ID |
| M8-09 | 明确缺陷 | ✅ 已修复 | service.py 已使用 `self._clock.now()`；PaperGenerator 已改为可注入时钟 |
| M8-10 | 明确缺陷 | ✅ 已修复 | getattr 防御模式已全部移除；apply_teacher_review 从 repository 读取权威审计做并发版本检测；SQLite 适配器已实现 |

---

## 二、阶段目标缺口实现汇总

| 编号 | 判定 | 状态 | 说明 |
|------|------|------|------|
| M5-08 | 阶段目标缺口 | ✅ 已实现 | DINA：按概念聚合观测得分率，输出掌握概率；零观测返回 empty |
| M5-09 | 阶段目标缺口 | ✅ 已实现 | BKT：标准四参数模型（P(L0)=0.1, P(T)=0.1, P(G)=0.25, P(S)=0.25），按概念时序更新 |
| M8-11 | 阶段目标缺口 | ✅ 已实现 | IRT 2PL shadow 标定：p-value 难度代理 + 默认区分度，输出 shadow 参数集 |
| M8-12 | 阶段目标缺口 | ✅ 已实现 | 自适应选题：策略/能力前置校验 + Fisher 信息量目标计算 + 能力透传 |

---

## 三、M5-07 和 M8-10 完全修复详情

### M5-07：Repository 被声明但服务完全没有使用

**审计原意**：服务应持久化学习者/班级快照和已处理审计水位，重启后可恢复。

**修复内容**：
1. **协议扩展**：`M5Repository` Protocol 新增 `get_latest_learner_state`、`get_latest_class_state`、`get_processed_audits`、`save_processed_audits` 方法
2. **去掉 getattr 防御模式**：`save_learner_state`、`save_class_state`、`save_processed_audits` 改为直接调用
3. **从 repository 读取恢复**：
   - `update_state` 开始时从 repository 加载已处理审计水位（`get_processed_audits`），而非仅依赖内存 `_processed_by_learner`
   - 当调用方未提供 `previous_learner_state_snapshot` 时，从 repository 读取最新版本（`get_latest_learner_state`）
   - 当调用方未提供 `previous_class_state_snapshot` 时，从 repository 读取最新版本（`get_latest_class_state`）
4. **SQLite 适配器**：新增 `infrastructure/sqlite/m5_repository.py`（`SQLiteM5Repository`），实现全部协议方法
5. **迁移 v4**：新增 `m5_processed_audits` 表

### M8-10：Repository 是可选调用，且没有实际持久化适配器

**审计原意**：试卷和评分审计应持久化，教师复核应读取权威当前版本。

**修复内容**：
1. **协议扩展**：`M8Repository` Protocol 新增 `get_paper`、`get_latest_score_audit` 方法
2. **去掉 getattr 防御模式**：`generate_paper`、`finalize_scoring`、`apply_teacher_review` 中的 `getattr(self._repository, ...)` 全部替换为直接调用
3. **并发版本检测**：`apply_teacher_review` 在执行复核前，从 repository 读取权威审计（`get_latest_score_audit`），若权威版本号高于调用方提供的版本号，抛出 `REVIEW_VERSION_CONFLICT`
4. **SQLite 适配器**：新增 `infrastructure/sqlite/m8_repository.py`（`SQLiteM8Repository`），实现全部协议方法
5. **迁移 v4**：新增 `m8_papers` 表

---

## 四、审计误判说明

经逐条核实，**未发现明确误判**。审计报告中的 17 条"明确缺陷"在审计时间点均成立。

需注意：M8-09 的审计证据引用了 `service.py` 多处使用 `FIXED_TIME` 的行号。在 M8-07 修复（commit 987ab49）中，service.py 已改用 `self._clock.now()`，因此审计证据中的部分行号可能已过时。但审计的核心判定（PaperGenerator 硬编码 `FIXED_TIME`）是准确的，已在本次修复中解决。
