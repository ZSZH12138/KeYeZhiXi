# M8 测评、评分、IRT 与自适应标定

## 负责人

童

## 职责

M8 按蓝图权重组卷，冻结试卷与量规，完成客观/主观评分编排、教师复核和
权威学习观测；同时负责 2PL IRT 标定、能力估计和受约束的自适应选题。

## 输入来源

- M4 `TaskPlan`、M3 `KnowledgeBundle`，以及可选的 M5 学习状态/诊断。
- M0 `AssessmentSubmission` 或已验证的 `student_answers.json`。
- M7 `RubricScoringResult`、M9 `TeacherReviewDecision`。
- M8 权威学习观测、`AdaptiveSelectionPolicy` 和题目曝光快照。

## 输出

- `AssessmentPaper`、`ScoringPreparationResult`、`ScoringResultBundle`。
- `LearningObservationBatch`：只包含最终有效的最新评分版本。
- `CalibrationRunResult` 与追加式 `IRTParameterSet`。
- `AbilityEstimate` 与 `AdaptiveSelectionResult`。

## IRT 启用顺序

1. 至少 200 名学生、10 道题、每题至少 50 个同时含对错的有效作答，才尝试
   2PL 标定；不足时返回 `INSUFFICIENT_CALIBRATION_DATA`。
2. 标定成功先生成 `shadow` 参数，不能直接用于正式能力估计或选题。
3. M9 检查收敛指标、样本覆盖、参数边界和信息量。
4. 只有质量报告为 `ready` 且教师批准，才追加 `approved` 参数版本。
5. 能力估计和自适应选题只接受 `approved` 参数；选题同时遵守概念配额、
   已作答排除、难度范围和曝光上限。

## 数据与恢复规则

- 试卷、冻结量规、评分审计、教师复核、参数版本、能力和选题结果均追加保存。
- 教师修改评分会创建新审计版本；旧版本不覆盖，过期复核会稳定报冲突。
- 服务重启后可恢复原试卷、评分、参数、能力和自适应选题结果。
- SQLite 与 PostgreSQL 使用相同的身份、校验和与状态规则。

## 禁止事项

不得覆盖历史、接受越界评分、让被拒绝评分进入 M5、用答对率冒充 2PL、
未经质量审核和教师批准发布参数、在 M8 调用 DeepSeek，或绕开选题约束。
