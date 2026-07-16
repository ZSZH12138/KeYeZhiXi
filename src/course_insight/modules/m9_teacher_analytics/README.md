# M9 教师分析、DeepSeek 叙述与模型质量

## 负责人

冯

## 职责

构建班级/个体报告、复核队列和证据化教学建议，评估 DINA/BKT/IRT 的模型
或参数版本质量，并记录教师对 M8 影子标定的审核决定。LLM 只允许使用
DeepSeek API，仅将已计算的结构化证据转写为教师叙述，不代替统计计算。
当前 DeepSeek 叙述是空实现，模型质量返回 `insufficient_data`。

## 输入来源

- M3 `KnowledgeBundle`、M8 `ScoringResultBundle`、M5 `StateUpdateResult`。
- M8 `CalibrationRunResult` 与未来 M5 `LearningModelRun`。
- M0 转换的 `TeacherReviewSubmission` 或经验证的 `teacher_review.json`。
- `LLMGenerationRequest`，用例必须为 `teacher_narrative`，provider 必须是 DeepSeek。

## 输出

- `TeacherAnalyticsBundle` 供 M0 Django 教师外层，`TeacherReviewDecision` 返回 M8。
- `LLMGenerationResult`：当前 `empty` 且 `not_run`。
- `ModelQualityReport`：样本或指标不足时只能为 `insufficient_data`。
- `CalibrationReviewDecision`：教师对影子标定 approve/reject/defer 的追加式记录。

## 禁止事项

不得在证据不足时生成质量指标/高置信建议、让 DeepSeek 重算统计、在空实现
中读取 `DEEPSEEK_API_KEY`或访问网络、自动发布 M8 参数、直接修改评分、
泄露身份，或跨模块读表。
