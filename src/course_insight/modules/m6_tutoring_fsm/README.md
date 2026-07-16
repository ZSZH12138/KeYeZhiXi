# M6 辅导状态机

## 负责人

陈

## 职责

约束 S0—S5 合法迁移，依据评分、诊断与学习状态选择下一教学动作，
并生成反馈任务和证据查询。M6 后续可消费 M5 DINA 认知诊断与 BKT
知识追踪的已审核契约，但状态机不自己运行模型。

## 输入来源

- M4 `TaskPlan`。
- M8 `ScoringResultBundle`。
- M5 `StateUpdateResult`，其中现有诊断是教学动作的直接依据。
- 前版 `SessionStateSnapshot`。

## 输出

`TutoringControlResult`：`EvidenceQuery` 进入 M2 RAG，
`FeedbackGenerationTask` 进入 M7 DeepSeek 边界，新会话快照由 M6 继续管理。

## 禁止事项

不得非法跳转、泄漏标准答案、跳过证据查询、在状态机内运行 DINA/BKT/
DeepSeek，或把未审核模型概率直接当作教学事实。
