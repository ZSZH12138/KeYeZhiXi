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
- M0 转换的 `AssessmentSubmission` 或经验证的 `student_answers.json`。
- M7 `RubricScoringResult`、M9 `TeacherReviewDecision`。
- `LearningObservationBatch`、`AdaptiveSelectionPolicy`和 `AbilityEstimate`。

## 输出

- `AssessmentPaper`、`ScoringPreparationResult`、`ScoringResultBundle`。
- `IRTParameterSet`：空、shadow、approved 或 rejected 的追加式参数版本。
- `CalibrationRunResult`：供 M9 质量门槛/审核；当前为 `empty`。
- `AdaptiveSelectionResult`：供出卷编排；当前题目列表为空。

## 禁止事项

不得覆盖审计/参数历史、接受越界分数或无证据正分、在数据不足时伪造
IRT 指标、未经 M9 审核发布参数、在 M8 调用 DeepSeek，或绕开蓝图/概念配额选题。
