# M9 教师分析、DeepSeek 叙述与模型质量

## 负责人

冯

## 职责

M9 构建班级/个体报告和教师复核决定，并对 M8 的 2PL shadow 标定生成可审核的
质量报告。DeepSeek 教师叙述边界仍不访问网络；模型质量计算是本地、确定性的，
不依赖 LLM。

## 输入来源

- M3 `KnowledgeBundle`、M8 `ScoringResultBundle`、M5 `StateUpdateResult`。
- M8 `CalibrationRunResult`。
- M0 `TeacherReviewSubmission` 或经验证的 `teacher_review.json`。
- `LLMGenerationRequest`；仅允许 `teacher_narrative` 和 DeepSeek provider。

## 输出

- `TeacherAnalyticsBundle`、`TeacherReviewDecision`。
- `ModelQualityReport`：检查收敛指标、样本覆盖、参数边界稳定性和平均信息量，
  输出 `ready`、`failed` 或 `insufficient_data`。
- `CalibrationReviewDecision`：教师对 shadow 标定作出 approve/reject/defer。
- `LLMGenerationResult`：DeepSeek 尚未启用时保持 `empty/not_run`。

质量报告只评估证据，不自动发布参数；M8 必须同时收到 `ready` 报告和教师批准，
才能创建 approved 参数版本。

## 禁止事项

不得在证据不足时伪造指标或高置信建议、让 DeepSeek 重算统计、自动发布 M8
参数、直接覆盖评分、泄露身份，或跨模块读表。
