# M6 辅导状态机

## 负责人

陈

## 职责

M6 使用本地确定性策略约束 S0—S5 的 8 条合法迁移，依据 M8 评分审计和
M5 诊断/学习状态选择下一教学动作，并生成可直接交给 M2、M7 的查询与反馈任务。
状态机不运行 DINA、BKT、IRT 或任何 LLM，也不发起网络请求。

真实应用链首次调用 M6 时没有历史快照。M6 以 `S1`、`turn_count=0` 建立会话
游标；S0 仍保留完整的合法迁移和测试语义。Repository 已有历史时，即使调用方
继续传入 `None`，M6 也会恢复最新权威快照。

## 决策规则

- S1 在教师待复核、活跃误区或前置缺口时进入 S2，否则进入 S3。
- S2 固定进入 S3，S3 固定进入 S4。
- S4 有补救信号时回到 S2；缺少新证据、仍有诊断误区或稳定指标未达标时回到
  S3；只有新证据、纠正率、掌握置信度和提示依赖全部达标时进入 S5。
- S5 为终态，任何继续迁移都返回 `INVALID_STATE_TRANSITION`。

目标依次取诊断优先概念、前置缺口、高优先补救目标和弱概念，稳定去重后只保留
当前诊断支持且具有 `ConceptState` 的概念。无可靠目标时返回
`TUTORING_REFERENCE_MISMATCH`，不再盲目选择首个概念状态。

## 输入与输出

- 输入：M4 `TaskPlan`、M8 `ScoringResultBundle`、M5 `StateUpdateResult`，以及
  可选的前版 `SessionStateSnapshot`。
- 输出：`TutoringControlResult`。其中 `TaskPlan.course_package_id` 原样进入 M2
  `EvidenceQuery`，`FeedbackGenerationTask` 直接进入 M7。
- 所有 action/query/feedback/decision ID 都由 canonical JSON 与 SHA-256 派生；
  查询自由文本不拼接概念 ID，教学动作恒为 `must_not_reveal_answer=True`。

## 持久化与并发

`m6_session_states` 追加保存每轮快照，`m6_tutoring_decisions` 保存请求指纹、权威
输入指纹、证据水位和完整结果。SQLite 适配器在 `BEGIN IMMEDIATE` 事务内执行
insert-or-get；相同请求重放、进程重启和 20 路并发只产生一个权威决策，不同输入
争用同一旧游标时拒绝陈旧一方，任何失败都会整体回滚。

## 禁止事项

不得非法跳转、覆盖历史、泄漏标准答案或真实身份、跳过证据查询、读取密钥、调用
DeepSeek/其他模型，或越权读写其他模块的数据表。
