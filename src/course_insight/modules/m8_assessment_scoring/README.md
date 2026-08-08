# M8 测评、评分、IRT 与自适应标定

## 负责人

童

## 职责

按蓝图组卷，完成客观评分、主观评分编排、审计合并和教师复核版本；
同时拥有 IRT 题目参数、能力估计、自适应选题和在线标定责任。在线标定
必须先产生 shadow 参数版本，通过 M9 模型质量报告和教师审核后才能启用。
当前 `calibrate_irt` 和 `select_adaptive_items` 是空实现，不拟合、不选题。

## 输入来源

- M4 `TaskPlan`、M3 `KnowledgeBundle`、M5 学习状态/诊断。
- M0 转换的 `AssessmentSubmission` 或经验证的 `student_answers.json`；二者由
  `prepare_scoring` 在同一边界消费，答案均归一为题目实例 ID 到 JSON 标量值。
- M7 `RubricScoringResult`、M9 `TeacherReviewDecision`。
- `LearningObservationBatch`、`AdaptiveSelectionPolicy`和 `AbilityEstimate`。

## 输出

- `AssessmentPaper`、`ScoringPreparationResult`、`ScoringResultBundle`。
- `IRTParameterSet`：空、shadow、approved 或 rejected 的追加式参数版本。
- `CalibrationRunResult`：供 M9 质量门槛/审核；当前为 `empty`。
- `AdaptiveSelectionResult`：供出卷编排；当前题目列表为空。

## 教师驳回语义

目标协议中，`reject` 会追加 `rejected_pending_rescore` 审计版本并保留原分数作为
历史证据，但该数字不再是可用成绩，也不会自动改成零分。当前候选已阻止 rejected
bundle 再次进入 M5，并让 M9 分数统计和学生界面隐藏该分数；`confirm` 不能恢复
已驳回评分，只有新评分或完整守恒的 `override` 可以恢复消费。

现有提交流程在教师复核前已经把初始评分写入 M5，因此完整撤销 mastery/class
影响仍需冻结跨模块补偿或延迟入账协议。该缺口记录在
`docs/m7_m9_cross_module_change_request.md`，在全局审核结论前不得宣称 reject 已完成
端到端回滚。

## 禁止事项

不得覆盖审计/参数历史、接受越界分数或无证据正分、在数据不足时伪造
IRT 指标、未经 M9 审核发布参数、在 M8 调用 DeepSeek，或绕开蓝图/概念配额选题。
