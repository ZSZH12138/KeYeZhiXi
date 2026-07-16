# M7 DeepSeek 评分与学生反馈边界

## 负责人

冯

## 职责

在教师量规、M2 课程证据和安全输出契约约束下执行主观评分与学生反馈。
所有 LLM 调用统一规定为 DeepSeek API，密钥只能在真实运行时通过
`DEEPSEEK_API_KEY` 提供。当前 `invoke_deepseek` 是空实现：不读密钥、不访问
网络，返回 `status=empty`、空内容、空引用和 `finish_reason=not_run`。

## 输入来源

- M8 `RubricScoringTask`。
- M6 `FeedbackGenerationTask`。
- M2 `EvidenceBundle` 和可选 `RetrievalAudit`。
- `LLMGenerationRequest`：仅允许 `rubric_scoring` 或 `student_feedback`，携带模板版本、证据 ID 和输入校验和。

## 输出

- 现有确定性 `RubricScoringResult` 返回 M8，`StudentFeedbackPackage` 返回 M0 学生外层。
- v2 `LLMGenerationResult`，provider 固定为 `deepseek`；当前只有空结果。
- 后续真实适配器同时产生 `ModelInvocationAudit` 与 `SafetyCheckResult`，但不留存完整提示词或密钥。

## 禁止事项

不得接入 DeepSeek 以外的 LLM、加载本地模型权重、硬编码密钥、在空实现
中发起网络请求、接受不匹配证据、引用不存在证据，或在学生反馈中泄漏答案。
